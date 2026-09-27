from __future__ import annotations

import copy
import inspect
import json
import hashlib
import math
import os
import re
import shutil
import stat
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from fastapi import HTTPException, UploadFile

from app.atomic_json import atomic_write_json, path_lock, read_json_object
from app.edit_ops import (
    EditOperationError,
    _build_find_candidates,
    _capture_nearby_text_context,
    _capture_selected_text_snapshot,
    _clear_current_cell_text,
    _delete_selection,
    _delete_ctrl,
    _enumerate_controls_headctrl,
    _get_ctrl_anchor_pos,
    _get_pos,
    _get_current_paragraph_text_at_cursor,
    _get_selected_pos,
    _get_selection_mode,
    _get_selected_text,
    _move_after_selection,
    _move_doc_begin,
    _move_to_field,
    _normalize_visible_text,
    _preview_text,
    _resolve_current_table_cell_for_replacement,
    _run_table_cell_action,
    _select_text,
    _select_current_cell_contents,
    _select_paragraph_with_trailing_break_for_current_selection,
    _select_whole_paragraph_for_current_selection,
    _selected_text_contains_probe,
    _selection_anchor_pos,
    _set_pos,
    _snapshot_cursor_context,
)
from app.local_cli_document import (
    LocalCliDocumentError,
    build_context,
    find_matches,
    load_paragraph_records,
    load_plain_text_records,
    resolve_match_target,
)
from app.local_cli_runtime import (
    _RECOVERY_STATES,
    _RECOVERY_ARTIFACT_KINDS,
    _artifact_kind_from_key,
    _bounded_journal_value,
    LocalCliRuntimeError,
    LocalCliRuntimeHandle,
    LocalCliRuntimeTimeoutError,
    apply_char_style,
    capture_screenshot_artifact,
    read_command_journal,
    create_table_at_cursor,
    ensure_session_layout,
    export_document_pdf,
    get_local_cli_runtime_manager,
    insert_multiline_text_at_caret_native,
    insert_text_at_caret,
    insert_numbered_list_at_cursor,
    save_document,
    snapshot_live_location,
)
from app.models import CellMarginsGetRequest, CellMarginsGetTarget, canonical_cell_margins_request_sha256
from app.local_cli_type_guard import type_insert_guard_reason
from app.command_packages.runtime import get_command_package_registry
from app.raw_readback import RawReadbackMismatch, build_raw_target_readback
from app.readiness import (
    build_plain_readiness_failure,
    load_runtime_readiness_snapshot,
    readiness_matches_current_worker,
    resolve_candidate_generation,
    utc_now_iso,
)
from app.worker import save_hwp_as


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


class LocalCliService:
    def __init__(self, *, settings: Any, interactive_sessions: Any):
        self.settings = settings
        self.interactive_sessions = interactive_sessions
        self.runtime_manager = get_local_cli_runtime_manager()
        self.root = settings.spool_root / 'local_cli_v1'
        self.sessions_root = self.root / 'sessions'
        self.active_binding_path = self.root / 'active_binding.json'
        self.command_packages = get_command_package_registry()
        self._closed_session_ids: set[str] = set()
        self._closed_session_ids_lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        # Exact managed custody binding reused by the cell-margins getter's
        # on-disk readback; populated per-call (see cell_margins_get).
        self._cell_margins_custody_binding: dict[str, Any] = {}

    def _binding_path(self, session_id: str) -> Path:
        return self.sessions_root / session_id / 'binding.json'

    def _default_session_root(self, session_id: str) -> Path:
        return self.sessions_root / session_id

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            return read_json_object(path)
        except ValueError as exc:
            raise LocalCliServiceError(
                f'Local CLI binding JSON could not be read safely: {path}',
                status_code=500,
            ) from exc

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            atomic_write_json(path, payload)
        except Exception as exc:
            raise LocalCliServiceError(
                f'Local CLI binding JSON could not be persisted atomically: {path}',
                status_code=500,
            ) from exc

    def _binding_session_id(self, binding: dict[str, Any]) -> str:
        session_id = str(binding.get('session_id') or '').strip()
        if not session_id:
            raise LocalCliServiceError('Local CLI session binding is missing session_id.', status_code=500)
        if (
            len(session_id) > 128
            or session_id != str(binding.get('session_id') or '')
            or session_id in {'.', '..'}
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', session_id) is None
        ):
            raise LocalCliServiceError('Local CLI session_id is not a safe binding-path identifier.', status_code=500)
        return session_id

    def _parse_binding_generation(self, value: Any, *, default: int = 0) -> int:
        raw = default if value is None else value
        try:
            generation = int(raw)
        except (TypeError, ValueError) as exc:
            raise LocalCliServiceError('Local CLI binding generation is invalid.', status_code=500) from exc
        if generation < 0:
            raise LocalCliServiceError('Local CLI binding generation is invalid.', status_code=500)
        return generation

    def _mark_session_closed(self, session_id: str) -> None:
        closed_ids = getattr(self, '_closed_session_ids', None)
        if closed_ids is None:
            closed_ids = set()
            self._closed_session_ids = closed_ids
        lock = getattr(self, '_closed_session_ids_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._closed_session_ids_lock = lock
        with lock:
            closed_ids.add(session_id)

    def _is_session_closed(self, session_id: str) -> bool:
        closed_ids = getattr(self, '_closed_session_ids', set())
        lock = getattr(self, '_closed_session_ids_lock', None)
        if lock is None:
            return session_id in closed_ids
        with lock:
            return session_id in closed_ids

    def _binding_session_root(self, binding: dict[str, Any]) -> Path:
        raw = str(binding.get('session_root_path') or '').strip()
        if raw:
            return Path(raw)
        return self._default_session_root(self._binding_session_id(binding))

    def _managed_path_identity(self, path: Path) -> dict[str, int] | None:
        """Return a no-follow filesystem identity for a server-owned root."""

        try:
            stat_result = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        return {
            'device': int(stat_result.st_dev),
            'inode': int(stat_result.st_ino),
            'mode': int(stat_result.st_mode),
        }

    def _path_has_symlink_component(self, path: Path) -> bool:
        """Check every lexical component without resolving through it."""

        lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
        current = Path(lexical.anchor)
        for part in lexical.parts[1:]:
            current = current / part
            try:
                if current.is_symlink():
                    return True
            except OSError:
                return True
        return False

    def _cleanup_managed_session_root(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Delete one owned session root and verify the exact root is gone."""

        raw_path = str(binding.get('session_root_path') or '').strip()
        session_id = self._binding_session_id(binding)
        if not raw_path or not session_id:
            raise LocalCliServiceError('Managed local CLI session root identity is incomplete.', status_code=500)
        root = Path(raw_path).expanduser()
        try:
            canonical_root = root.resolve(strict=False)
            canonical_parent = self.sessions_root.resolve(strict=True)
            canonical_root.relative_to(canonical_parent)
        except (OSError, ValueError) as exc:
            raise LocalCliServiceError('Managed local CLI session root is outside the server session store.', status_code=500) from exc
        if (
            canonical_root == canonical_parent
            or canonical_root.parent != canonical_parent
            or canonical_root.name != session_id
            or root.is_symlink()
            or self.sessions_root.is_symlink()
            or any(part in {'.', '..'} for part in root.parts)
            or root.parent.is_symlink()
            or self._path_has_symlink_component(self.sessions_root)
            or self._path_has_symlink_component(root)
            or root.parent.resolve(strict=True) != canonical_parent
        ):
            raise LocalCliServiceError('Managed local CLI session root is not a removable child directory.', status_code=500)
        for ancestor in (canonical_parent, canonical_root):
            if ancestor.is_symlink():
                raise LocalCliServiceError('Managed local CLI session root contains a symlink.', status_code=500)
        expected_identity = binding.get('session_root_identity')
        actual_identity = self._managed_path_identity(canonical_root)
        if actual_identity is None:
            raise LocalCliServiceError(
                'Managed local CLI session root is missing; cleanup ownership cannot be verified.',
                status_code=409,
            )
        if not isinstance(expected_identity, dict) or actual_identity != expected_identity:
            raise LocalCliServiceError('Managed local CLI session root identity changed; refusing cleanup.', status_code=409)
        identity_at_delete = self._managed_path_identity(canonical_root)
        if identity_at_delete != expected_identity:
            raise LocalCliServiceError('Managed local CLI session root identity changed before cleanup.', status_code=409)
        actual_identity = identity_at_delete
        shutil.rmtree(canonical_root)
        if canonical_root.exists() or canonical_root.is_symlink():
            raise LocalCliServiceError('Managed local CLI session root remained after cleanup.', status_code=500)
        return {
            'path': str(canonical_root),
            'removed': True,
            'verified': True,
            'already_absent': False,
            'object_identity': actual_identity,
        }

    def _read_binding(self, *, session_id: str | None = None) -> dict[str, Any] | None:
        if session_id:
            return self._read_json(self._binding_path(session_id))
        return self._read_json(self.active_binding_path)

    def _save_binding(self, binding: dict[str, Any]) -> dict[str, Any]:
        session_id = self._binding_session_id(binding)
        session_path = self._binding_path(session_id)
        expected_raw = binding.get('_expected_command_generation', binding.get('command_generation', 0))
        expected_generation = self._parse_binding_generation(expected_raw)
        native_sequence_present = 'native_command_sequence' in binding
        expected_native_raw = binding.get('_expected_native_command_sequence')
        expected_native_sequence = (
            self._parse_binding_generation(expected_native_raw)
            if expected_native_raw is not None
            else None
        )
        native_sequence = self._parse_binding_generation(binding.get('native_command_sequence', 0))
        payload = dict(binding)
        payload.pop('_expected_command_generation', None)
        payload.pop('_expected_native_command_sequence', None)
        base_binding = payload.pop('_binding_base', None)
        with path_lock(self.root / '.binding-state.lock'):
            current = self._read_json(session_path)
            if self._is_session_closed(session_id):
                raise LocalCliServiceError(
                    'Local CLI binding was cleared for this closed session; refusing to resurrect it.',
                    status_code=409,
                )
            current_generation = self._parse_binding_generation((current or {}).get('command_generation', 0))
            current_native_sequence = self._parse_binding_generation((current or {}).get('native_command_sequence', 0))
            if current is not None and 'native_command_sequence' in current and not native_sequence_present:
                raise LocalCliServiceError(
                    'Local CLI binding projection is missing the native command sequence; refusing a stale write.',
                    status_code=409,
                )
            if current is None and expected_generation != 0:
                raise LocalCliServiceError(
                    'Local CLI binding was cleared while this command was running; refusing to resurrect a stale generation.',
                    status_code=409,
                )
            if native_sequence_present:
                if expected_native_sequence is None:
                    expected_native_sequence = current_native_sequence
                if current is None and expected_native_sequence != 0:
                    raise LocalCliServiceError(
                        'Local CLI binding was cleared while a native command was running; refusing a stale sequence.',
                        status_code=409,
                    )
                if current is not None and current_native_sequence != expected_native_sequence:
                    raise LocalCliServiceError(
                        'Local CLI native command sequence conflict; refusing a stale projection.',
                        status_code=409,
                    )
                latest_sequence_getter = getattr(self.runtime_manager, 'latest_command_sequence', None)
                if callable(latest_sequence_getter):
                    try:
                        latest_sequence = int(
                            latest_sequence_getter(
                                session_id,
                                session_root=self._binding_session_root(binding),
                            )
                        )
                    except Exception:
                        latest_sequence = 0
                    if latest_sequence > native_sequence:
                        raise LocalCliServiceError(
                            'Local CLI native command sequence is stale; refusing an older projection.',
                            status_code=409,
                        )
            if current is not None and current_generation != expected_generation:
                if not isinstance(base_binding, dict):
                    raise LocalCliServiceError(
                        'Local CLI binding generation conflict; refusing to overwrite a newer native command result.',
                        status_code=409,
                    )
                # The native command queue may have advanced while this
                # request was extracting its post-command location. Merge
                # only fields this request actually changed onto the newer
                # committed binding; never replay its stale full snapshot.
                changed = {
                    key: copy.deepcopy(value)
                    for key, value in payload.items()
                    if base_binding.get(key) != value
                }
                payload = dict(current)
                payload.update(changed)
                expected_generation = current_generation
            active = self._read_json(self.active_binding_path)
            if active is not None:
                active_session_id = str(active.get('session_id') or '').strip()
                if active_session_id and active_session_id != session_id:
                    raise LocalCliServiceError(
                        'Local CLI active binding belongs to another session; refusing to overwrite it.',
                        status_code=409,
                    )
                active_generation = self._parse_binding_generation(active.get('command_generation', 0))
                if active_session_id == session_id and active_generation != expected_generation:
                    raise LocalCliServiceError(
                        'Local CLI active binding generation conflict; refusing a stale projection.',
                        status_code=409,
                    )
            payload['command_generation'] = expected_generation + 1
            if native_sequence_present:
                payload['native_command_sequence'] = native_sequence
            previous_session = current
            previous_active = active
            try:
                self._write_json(session_path, payload)
                self._write_json(self.active_binding_path, payload)
                session_readback = self._read_json(session_path)
                active_readback = self._read_json(self.active_binding_path)
                if session_readback != payload or active_readback != payload:
                    raise LocalCliServiceError(
                        'Local CLI binding projection readback did not match the committed generation.',
                        status_code=500,
                    )
            except Exception as exc:
                # The two projections are one logical commit. If the second
                # replace or either readback fails, restore both preimages
                # under the same lock rather than leaving a split generation.
                try:
                    for projection_path, previous in (
                        (session_path, previous_session),
                        (self.active_binding_path, previous_active),
                    ):
                        if previous is None:
                            if projection_path.exists():
                                projection_path.unlink()
                            if projection_path.exists():
                                raise OSError(f'Binding rollback left a projection behind: {projection_path}')
                        else:
                            self._write_json(projection_path, previous)
                except Exception as rollback_exc:
                    raise LocalCliServiceError(
                        'Local CLI binding commit failed and projection rollback was incomplete.',
                        status_code=500,
                    ) from rollback_exc
                if isinstance(exc, LocalCliServiceError):
                    raise
                raise LocalCliServiceError(
                    'Local CLI binding projection commit failed; previous generation was restored.',
                    status_code=500,
                ) from exc
        binding.clear()
        binding.update(payload)
        return binding

    def _clear_binding(
        self,
        *,
        binding: dict[str, Any] | None = None,
        session_id: str | None = None,
        force: bool = False,
    ) -> None:
        resolved_session_id = session_id
        if resolved_session_id is None and isinstance(binding, dict):
            resolved_session_id = str(binding.get('session_id') or '').strip() or None
        expected_generation: int | None = None
        if isinstance(binding, dict) and '_expected_command_generation' in binding:
            expected_generation = self._parse_binding_generation(binding['_expected_command_generation'])
        elif isinstance(binding, dict) and 'command_generation' in binding:
            expected_generation = self._parse_binding_generation(binding['command_generation'])
        expected_native_sequence: int | None = None
        if isinstance(binding, dict) and 'native_command_sequence' in binding:
            expected_native_sequence = self._parse_binding_generation(binding['native_command_sequence'])

        with path_lock(self.root / '.binding-state.lock'):
            if session_id is None and binding is None:
                if self.active_binding_path.exists():
                    self.active_binding_path.unlink()
                return

            current = self._read_json(self._binding_path(str(resolved_session_id))) if resolved_session_id else None
            if isinstance(current, dict):
                current_generation = self._parse_binding_generation(current.get('command_generation', 0))
                current_native_sequence = self._parse_binding_generation(current.get('native_command_sequence', 0))
                generation_matches = force or expected_generation is None or current_generation == expected_generation
                sequence_matches = (
                    expected_native_sequence is None
                    or current_native_sequence == expected_native_sequence
                    if 'native_command_sequence' in current
                    else expected_native_sequence is None
                )
                if generation_matches and sequence_matches:
                    binding_path = self._binding_path(str(resolved_session_id))
                    if binding_path.exists():
                        binding_path.unlink()

            active = self._read_json(self.active_binding_path)
            if not isinstance(active, dict):
                return
            active_session_id = str(active.get('session_id') or '').strip()
            active_generation = self._parse_binding_generation(active.get('command_generation', 0))
            active_native_sequence = self._parse_binding_generation(active.get('native_command_sequence', 0))
            if (
                (not resolved_session_id or active_session_id == resolved_session_id)
                and (force or expected_generation is None or active_generation == expected_generation)
                and (
                    expected_native_sequence is None
                    or active_native_sequence == expected_native_sequence
                    if 'native_command_sequence' in active
                    else expected_native_sequence is None
                )
                and self.active_binding_path.exists()
            ):
                self.active_binding_path.unlink()

    def _record_session_close(
        self,
        *,
        session_id: str,
        summary: str,
        outcome: str,
        state: str = 'succeeded',
    ) -> None:
        try:
            self.interactive_sessions.record_command(
                'close',
                session_id=session_id,
                state=state,
                summary=summary,
                payload={'outcome': outcome},
                metadata={'local_cli_v1': {'closed_via': 'local_cli_v1', 'outcome': outcome}},
                session_state='closed',
            )
        except Exception:
            pass

    def _cleanup_stale_binding(
        self,
        binding: dict[str, Any],
        *,
        summary: str = 'Local CLI live document session is no longer available.',
        outcome: str = 'stale',
    ) -> bool:
        session_id = self._binding_session_id(binding)
        if (
            self._binding_has_pending_reconciliation(binding)
            or binding.get('document_session_state') in {
                'reconciled', 'reconciled_cleanup_pending', 'closed_cleanup_pending'
            }
            or isinstance(binding.get('artifact_custody'), dict)
        ):
            return False
        # A missing runtime is not proof that the managed document was
        # released.  Preserve its binding/root so a restart or operator can
        # inspect the last custody evidence instead of deleting the only copy.
        has_session = getattr(self.runtime_manager, 'has_session', None)
        if callable(has_session):
            try:
                if not has_session(session_id):
                    return False
            except Exception:
                return False
        try:
            self.runtime_manager.close_session(session_id)
        except Exception:
            # Do not remove the managed session root while native/COM teardown
            # is uncertain.  The binding remains the ownership record for a
            # later retry or operator inspection.
            return False
        try:
            self._cleanup_managed_session_root(binding)
        except Exception:
            # Retain the binding when ownership cleanup cannot be proven; a
            # later reconciliation/operator pass must still be able to find
            # the server-managed root.
            return False
        self._record_session_close(session_id=session_id, summary=summary, outcome=outcome)
        self._clear_binding(binding=binding)
        self._mark_session_closed(session_id)
        return True

    def _command_status_for_binding(
        self,
        binding: dict[str, Any],
        command_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = self._binding_session_id(binding)
        session_root = self._binding_session_root(binding)
        try:
            return self.runtime_manager.command_status(
                session_id,
                command_id,
                session_root=session_root,
            )
        except Exception:
            try:
                return read_command_journal(session_root, command_id)
            except Exception:
                return {
                    'command_id': command_id,
                    'state': 'unknown',
                    'reconcilable': False,
                }

    def _binding_has_pending_reconciliation(self, binding: dict[str, Any]) -> bool:
        pending = binding.get('pending_command') if isinstance(binding.get('pending_command'), dict) else None
        if not pending:
            return False
        command_id = str(pending.get('command_id') or '').strip()
        if not command_id:
            return True
        # The persisted binding pointer is authoritative until this service
        # has projected the terminal result and removed it.  A journal entry
        # may already be marked reconciled after a retry, but clearing the
        # pointer before the binding projection is still unsafe: a projection
        # failure must keep normal work and cleanup blocked.
        return True

    def _looks_like_stale_live_session_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        stale_markers = (
            '-2147023179',  # 0x800706b5
            '-2147023174',  # 0x800706ba
            '0x800706b5',
            '0x800706ba',
            'interface unknown',
            'rpc server is unavailable',
            'rpc 서버를 사용할 수 없습니다',
            '원격 프로시저를 호출하지 못했습니다',
        )
        return any(marker in message for marker in stale_markers)

    def _probe_live_binding(self, binding: dict[str, Any], *, timeout: float = 5.0) -> bool:
        session_id = self._binding_session_id(binding)
        if not self.runtime_manager.has_session(session_id):
            return False

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            return snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
                include_nearby_context=False,
                include_document_snapshot=False,
            )

        try:
            location = self.runtime_manager.execute(
                session_id=session_id,
                command_name='health_probe',
                handler=_handler,
                timeout=timeout,
            )
        except LocalCliRuntimeTimeoutError as exc:
            # A probe timeout owns a real native command just like an edit
            # timeout.  Project its identity before any stale cleanup so the
            # operator can reconcile the late result and no session can be
            # deleted/reused while COM work is still in flight.
            try:
                command_status = self.runtime_manager.command_status(session_id, exc.command_id)
            except Exception:
                command_status = {'command_id': exc.command_id, 'state': exc.command_state}
            try:
                current_sequence = self._parse_binding_generation(binding.get('native_command_sequence', 0))
            except LocalCliServiceError:
                current_sequence = 0
            try:
                command_sequence = max(current_sequence, int(command_status.get('sequence', current_sequence)))
            except (TypeError, ValueError):
                command_sequence = current_sequence
            pending = {
                'command_id': exc.command_id,
                'command': 'health_probe',
                'sequence': command_sequence,
                'state': command_status.get('state', exc.command_state),
                'timed_out_at': utc_now_iso(),
            }
            binding['native_command_sequence'] = command_sequence
            binding['pending_command'] = pending
            binding['document_session_state'] = 'timed_out_pending_reconciliation'
            binding['live_session_bound'] = True
            binding['updated_at'] = utc_now_iso()
            self._save_binding(binding)
            try:
                self.interactive_sessions.record_command(
                    'health_probe',
                    session_id=session_id,
                    state='pending',
                    summary='health_probe timed out; awaiting native reconciliation',
                    payload={'command_id': exc.command_id, 'sequence': command_sequence},
                    metadata={'local_cli_v1': {'reconciliation_pending': True}},
                    live_runtime={'reconciliation_pending': True, 'pending_command': dict(pending)},
                )
            except Exception:
                pass
            return False
        except LocalCliRuntimeError as exc:
            if self._looks_like_stale_live_session_error(exc):
                self._cleanup_stale_binding(
                    binding,
                    summary='Local CLI live session became stale after the Hancom bridge stopped responding.',
                    outcome='stale',
                )
            return False

        try:
            current_sequence = self._parse_binding_generation(binding.get('native_command_sequence', 0))
        except LocalCliServiceError:
            current_sequence = 0
        command_sequence: int | None = None
        try:
            command_status = self.runtime_manager.command_status(session_id)
            command_sequence = int(command_status.get('sequence'))
        except (AttributeError, TypeError, ValueError, LocalCliRuntimeError):
            latest_sequence_getter = getattr(self.runtime_manager, 'latest_command_sequence', None)
            if callable(latest_sequence_getter):
                try:
                    command_sequence = int(latest_sequence_getter(session_id))
                except (TypeError, ValueError, LocalCliRuntimeError):
                    command_sequence = None
        if command_sequence is not None and command_sequence >= current_sequence:
            binding['_expected_native_command_sequence'] = current_sequence
            binding['native_command_sequence'] = command_sequence
        if isinstance(location, dict):
            binding = self._update_live_binding(binding, location=location)
            self._save_binding(binding)
        return True

    def _load_active_binding(self, *, session_id: str | None = None, require_live: bool = True) -> dict[str, Any]:
        binding = self._read_binding(session_id=session_id)
        if not isinstance(binding, dict):
            if session_id:
                raise LocalCliServiceError(f'Local CLI session binding not found: {session_id}', status_code=404)
            raise LocalCliServiceError('No active local CLI document is open.', status_code=404)

        resolved_session_id = self._binding_session_id(binding)
        if require_live and self._binding_has_pending_reconciliation(binding):
            pending = binding.get('pending_command') if isinstance(binding.get('pending_command'), dict) else {}
            command_id = str(pending.get('command_id') or '').strip()
            raise LocalCliServiceError(
                'A native local CLI command is awaiting reconciliation; '
                f'use command-reconcile for command_id={command_id}.',
                status_code=409,
            )
        if require_live and binding.get('live_session_bound') is False:
            raise LocalCliServiceError(
                'The local CLI session is no longer live; close it and open a new managed copy.',
                status_code=409,
            )
        if require_live and not self.runtime_manager.has_session(resolved_session_id):
            self._cleanup_stale_binding(binding)
            raise LocalCliServiceError('Live local CLI session is unavailable. Re-open the document.', status_code=409)
        return binding

    def _runtime_snapshot(self) -> dict[str, Any] | None:
        snapshot = load_runtime_readiness_snapshot()
        return snapshot if isinstance(snapshot, dict) else None

    def _require_ready_runtime(self, task_label: str) -> dict[str, Any]:
        snapshot = self._runtime_snapshot()
        if not readiness_matches_current_worker(
            snapshot,
            candidate_generation=resolve_candidate_generation(),
        ):
            raise LocalCliServiceError(build_plain_readiness_failure(task_label), status_code=503)
        return snapshot

    def _working_copy_path(self, binding: dict[str, Any]) -> Path:
        path = Path(str(binding.get('working_copy_path') or ''))
        if not path.exists() or not path.is_file():
            raise LocalCliServiceError('Active working copy is missing on the server.', status_code=404)
        return path

    def _validate_image_suffix(self, filename: str) -> str:
        suffix = Path(filename or '').suffix.lower()
        if suffix not in _IMAGE_ALLOWED_SUFFIXES:
            allowed = ', '.join(sorted(_IMAGE_ALLOWED_SUFFIXES))
            raise LocalCliServiceError(f'Unsupported image type: {suffix or "<none>"}. Allowed: {allowed}', status_code=400)
        return suffix

    def _parse_on_off_option(self, value: str | bool | None, *, field_name: str) -> bool | None:
        if value is None or value == '':
            return None
        if isinstance(value, bool):
            return value
        raw = str(value).strip().casefold()
        if raw in {'on', 'true', 'yes', '1'}:
            return True
        if raw in {'off', 'false', 'no', '0'}:
            return False
        raise LocalCliServiceError(f'{field_name} must be on or off.', status_code=400)

    def _normalize_image_options(
        self,
        *,
        width: float | None,
        height: float | None,
        sizeoption: int | None,
        treat_as_char: str | bool | None,
        embedded: str | bool | None,
        fit_cell: bool,
    ) -> dict[str, Any]:
        if width is not None and (isinstance(width, bool) or float(width) <= 0):
            raise LocalCliServiceError('width must be a positive number.', status_code=400)
        if height is not None and (isinstance(height, bool) or float(height) <= 0):
            raise LocalCliServiceError('height must be a positive number.', status_code=400)
        if sizeoption is not None and (isinstance(sizeoption, bool) or int(sizeoption) < 0):
            raise LocalCliServiceError('sizeoption must be a non-negative integer.', status_code=400)

        parsed_treat_as_char = self._parse_on_off_option(treat_as_char, field_name='treat_as_char')
        parsed_embedded = self._parse_on_off_option(embedded, field_name='embedded')
        resolved_sizeoption = int(sizeoption) if sizeoption is not None else (3 if fit_cell else None)

        # Default to embedded/as-character insertion for predictable document portability
        # and table-cell behavior. `--fit-cell` only adds the size-option shorthand.
        return {
            'width': float(width) if width is not None else None,
            'height': float(height) if height is not None else None,
            'sizeoption': resolved_sizeoption,
            'treat_as_char': True if parsed_treat_as_char is None else parsed_treat_as_char,
            'embedded': True if parsed_embedded is None else parsed_embedded,
            'fit_cell': bool(fit_cell),
        }

    async def _stage_image_upload(self, *, file: UploadFile, binding: dict[str, Any]) -> dict[str, Any]:
        raw_filename = Path(file.filename or 'image').name
        suffix = self._validate_image_suffix(raw_filename)
        safe_name = _clean_asset_filename(raw_filename, default_stem='image')
        safe_stem = Path(safe_name).stem or 'image'
        staged_filename = f'{safe_stem}-{uuid.uuid4().hex[:8]}{suffix}'
        asset_dir = self._binding_session_root(binding) / 'assets'
        asset_dir.mkdir(parents=True, exist_ok=True)
        staged_path = asset_dir / staged_filename
        size_bytes = 0

        try:
            with staged_path.open('wb') as target:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.settings.max_upload_mb * 1024 * 1024:
                        raise LocalCliServiceError('Image upload exceeds configured size limit.', status_code=413)
                    target.write(chunk)
            if size_bytes <= 0:
                raise LocalCliServiceError('Empty image upload is not allowed.', status_code=400)
        except Exception:
            try:
                if staged_path.exists():
                    staged_path.unlink()
            finally:
                raise

        return {
            'original_filename': raw_filename,
            'staged_filename': staged_filename,
            'staged_path': staged_path,
            'size_bytes': size_bytes,
        }

    def _insert_picture_with_available_method(
        self,
        hwp: Any,
        *,
        image_path: Path,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        option_kwargs = {
            'treat_as_char': bool(options.get('treat_as_char')),
            'embedded': bool(options.get('embedded')),
        }
        for key in ('sizeoption', 'width', 'height'):
            if options.get(key) is not None:
                option_kwargs[key] = options.get(key)

        last_error: Exception | None = None
        for method_name in ('insert_picture', 'InsertPicture'):
            method = getattr(hwp, method_name, None)
            if not callable(method):
                continue
            positional_options: list[Any] = [option_kwargs['treat_as_char'], option_kwargs['embedded']]
            if options.get('sizeoption') is not None:
                positional_options.append(options.get('sizeoption'))
            if options.get('width') is not None or options.get('height') is not None:
                positional_options.extend([options.get('width'), options.get('height')])
            attempts: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
                ('full-options', (), option_kwargs),
                ('size-options', (), {key: value for key, value in option_kwargs.items() if key in {'sizeoption', 'width', 'height'}}),
                ('full-positional', tuple(positional_options), {}),
                ('path-only', (), {}),
            ]
            for attempt_mode, extra_args, kwargs in attempts:
                try:
                    raw_result = method(str(image_path), *extra_args, **kwargs)
                except TypeError as exc:
                    last_error = exc
                    continue
                except Exception as exc:
                    raise LocalCliRuntimeError(f'{method_name} failed: {exc}') from exc
                if raw_result is False:
                    last_error = LocalCliRuntimeError(f'{method_name} returned false')
                    continue
                return {
                    'method': method_name,
                    'attempt_mode': attempt_mode,
                    'result_type': type(raw_result).__name__,
                    'result_preview': self._macro_result_preview(raw_result),
                }

        if last_error is not None:
            raise LocalCliRuntimeError(f'Image insertion method rejected the provided arguments: {last_error}') from last_error
        raise LocalCliRuntimeError('No HWP image insertion method is available (insert_picture/InsertPicture).')

    def _normalize_anchor_insert_position(self, value: Any) -> str:
        raw = str(value or 'before-anchor').strip().lower().replace('_', '-').replace(' ', '-')
        aliases = {
            'before': 'before-anchor',
            'before-anchor': 'before-anchor',
            'insert-before-anchor': 'before-anchor',
            'before-heading': 'before-heading',
            'insert-before-heading': 'before-heading',
            'after': 'after-anchor',
            'after-anchor': 'after-anchor',
            'insert-after-anchor': 'after-anchor',
            'after-paragraph': 'after-paragraph',
            'insert-after-paragraph': 'after-paragraph',
        }
        position = aliases.get(raw)
        if position is None:
            raise LocalCliServiceError(
                'position must be one of before-anchor, after-anchor, after-paragraph, before-heading',
                status_code=400,
            )
        return position

    def _selection_end_pos(self, snapshot: dict[str, Any] | None) -> tuple[int, int, int] | None:
        if not isinstance(snapshot, dict):
            return None
        selected = snapshot.get('selected_pos')
        if not (isinstance(selected, list) and len(selected) >= 7 and selected[0]):
            return None
        try:
            return (int(selected[4]), int(selected[5]), int(selected[6]))
        except Exception:
            return None

    def _move_to_anchor_insert_position(self, hwp: Any, *, target: str, position: str) -> dict[str, Any]:
        query = str(target or '').strip()
        if not query:
            raise LocalCliServiceError('target must not be empty', status_code=400)
        normalized_position = self._normalize_anchor_insert_position(position)
        match = self._find_live_match(hwp, query=query, occurrence=1)
        snapshot = match.get('snapshot') if isinstance(match.get('snapshot'), dict) else {}
        start_pos = _selection_anchor_pos(snapshot)
        end_pos = self._selection_end_pos(snapshot)

        if normalized_position in {'before-anchor', 'before-heading'}:
            cursor_pos = start_pos
        else:
            cursor_pos = end_pos or start_pos
        if cursor_pos is None:
            raise LocalCliRuntimeError('Failed to resolve the live cursor position for the anchor match.')
        _set_pos(hwp, cursor_pos[0], cursor_pos[1], cursor_pos[2])

        paragraph_break_required = False
        if normalized_position == 'after-paragraph':
            moved_to_line_end = False
            for method_name in ('MoveParaEnd', 'MoveLineEnd', 'MoveSelLineEnd'):
                method = getattr(hwp, method_name, None)
                if callable(method):
                    try:
                        raw = method()
                    except Exception:
                        continue
                    moved_to_line_end = raw is None or bool(raw)
                    if moved_to_line_end:
                        break
            run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if not moved_to_line_end and callable(run):
                for action_name in ('MoveParaEnd', 'MoveLineEnd'):
                    try:
                        raw = run(action_name)
                        moved_to_line_end = raw is None or bool(raw)
                    except Exception:
                        moved_to_line_end = False
                    if moved_to_line_end:
                        break
            paragraph_break_required = True

        return {
            'query': query,
            'position': normalized_position,
            'matched_query': match.get('matched_query'),
            'match_strategy': match.get('match_strategy'),
            'selected_text_preview': _preview_text(match.get('selected_text'), limit=120),
            'anchor_start_pos': list(start_pos) if start_pos is not None else None,
            'anchor_end_pos': list(end_pos) if end_pos is not None else None,
            'resolved_insert_pos': list(cursor_pos),
            'line_end_attempted': normalized_position == 'after-paragraph',
            'paragraph_break_required': paragraph_break_required,
        }

    def _anchor_insert_text_from_step(self, step: Mapping[str, Any]) -> str:
        fragments = step.get('fragments')
        if isinstance(fragments, list):
            return ''.join(str(item) for item in fragments)
        return str(step.get('text') or '')

    def _perform_anchor_insert(
        self,
        hwp: Any,
        *,
        target: str,
        position: str,
        text: str,
        session_root: Path,
    ) -> dict[str, Any]:
        normalized_position = self._normalize_anchor_insert_position(position)
        insert_text = str(text or '')
        if not insert_text:
            raise LocalCliRuntimeError('anchor_insert requires non-empty text')
        before = self._bundle_compact_snapshot(hwp)
        anchor = self._move_to_anchor_insert_position(hwp, target=target, position=normalized_position)
        effective_text = insert_text
        paragraph_break_inserted = False
        if normalized_position == 'after-paragraph':
            self._break_paragraph(hwp)
            paragraph_break_inserted = True
        strategy = self._insert_text_file_at_caret(hwp, text=effective_text, session_root=session_root)
        after = self._bundle_compact_snapshot(hwp)
        context = _capture_nearby_text_context(hwp)
        marker = next((line.strip() for line in effective_text.splitlines() if line.strip()), '')
        warnings: list[str] = []
        if normalized_position == 'after-paragraph':
            warnings.append('after-paragraph uses native paragraph-end movement plus BreakPara before insertion so the new text is not concatenated to the anchor paragraph.')
        return {
            'schema_version': 'local-cli/anchor-insert/v1',
            'target': target,
            'position': normalized_position,
            'anchor': anchor,
            'text_len': len(effective_text),
            'text_hash': self._text_proof_hash(effective_text),
            'inserted_after_anchor': marker or f'sha256:{self._text_proof_hash(effective_text)}',
            'strategy': strategy,
            'paragraph_break_inserted': paragraph_break_inserted,
            'before': before,
            'after': after,
            'context': context,
            'warnings': warnings,
        }

    def _format_figure_section_text(self, *, heading: str, intro: str | None, caption: str | None, body: str | None) -> tuple[str, str]:
        before_image_parts = [str(heading or '').strip()]
        if intro and str(intro).strip():
            before_image_parts.append(str(intro).strip())
        after_image_parts = []
        if caption and str(caption).strip():
            after_image_parts.append(str(caption).strip())
        if body and str(body).strip():
            after_image_parts.append(str(body).strip())
        before_image = '\n'.join(part for part in before_image_parts if part)
        after_image = '\n'.join(after_image_parts)
        return (before_image + '\n') if before_image else '', ('\n' + after_image + '\n') if after_image else ''

    def _capture_current_control_id(self, hwp: Any) -> dict[str, Any]:
        ctrl = getattr(hwp, 'CurSelectedCtrl', None)
        if ctrl is None:
            return {'warning': 'CurSelectedCtrl unavailable after image insertion; using textual anchors for proof.'}
        getter = getattr(ctrl, 'GetCtrlInstID', None)
        if callable(getter):
            try:
                value = getter()
                return {'ctrl_inst_id': str(value)}
            except Exception as exc:
                return {'warning': f'CurSelectedCtrl.GetCtrlInstID failed: {exc}; using textual anchors for proof.'}
        return {'warning': 'CurSelectedCtrl.GetCtrlInstID unavailable; using textual anchors for proof.'}

    def _normalize_figure_text_field(self, value: Any, *, field_name: str, required: bool = False, max_chars: int = _MACRO_MAX_STRING_CHARS) -> str:
        text = str(value or '').strip()
        if required and not text:
            raise LocalCliServiceError(f'{field_name} must not be empty', status_code=400)
        if len(text) > max_chars:
            raise LocalCliServiceError(f'{field_name} is too long', status_code=400)
        return text

    def _normalize_cursor_pos(self, value: Any) -> tuple[int, int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except Exception:
            return None

    def _normalize_selected_range(self, value: Any) -> tuple[Any, ...] | None:
        if not isinstance(value, (list, tuple)) or len(value) < 7:
            return None
        if not bool(value[0]):
            return None
        return tuple(value)

    def _update_binding_from_snapshot(
        self,
        binding: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        clear_last_find: bool = False,
    ) -> dict[str, Any]:
        cursor_pos = _selection_anchor_pos(snapshot)
        if cursor_pos is None:
            cursor_pos = self._normalize_cursor_pos(snapshot.get('pos'))
        binding['cursor_pos'] = list(cursor_pos) if cursor_pos is not None else None

        selected_range = snapshot.get('selected_pos')
        binding['selected_range'] = list(selected_range) if isinstance(selected_range, list) and selected_range and selected_range[0] else None
        binding['current_cell_addr'] = snapshot.get('cell_addr')
        binding['updated_at'] = utc_now_iso()
        if clear_last_find:
            binding['last_find'] = None
        return binding

    def _update_live_binding(
        self,
        binding: dict[str, Any],
        *,
        location: dict[str, Any],
        artifacts: dict[str, Any] | None = None,
        dirty: bool | None = None,
        clear_last_find: bool = False,
        clear_selection_cache: bool = False,
    ) -> dict[str, Any]:
        cursor = location.get('cursor') if isinstance(location.get('cursor'), dict) else None
        binding['last_live_location'] = location
        binding['last_cursor_snapshot'] = cursor
        if not self._binding_has_pending_reconciliation(binding):
            binding['document_session_state'] = 'open'
        binding['live_session_bound'] = self.runtime_manager.has_session(self._binding_session_id(binding))
        if isinstance(cursor, dict):
            binding = self._update_binding_from_snapshot(binding, cursor, clear_last_find=clear_last_find)
        elif clear_last_find:
            binding['last_find'] = None
        if isinstance(artifacts, dict) and artifacts:
            current = binding.get('artifacts') if isinstance(binding.get('artifacts'), dict) else {}
            current.update(artifacts)
            binding['artifacts'] = current
        if dirty is None and isinstance(location.get('document_is_modified'), bool):
            dirty = bool(location.get('document_is_modified'))
        if dirty is not None:
            binding['working_copy_dirty'] = dirty
        if clear_selection_cache:
            binding['selected_range'] = None
            binding['last_selection'] = None
            binding['unsafe_selection_for_type'] = None
        binding['updated_at'] = utc_now_iso()
        return self._save_binding(binding)

    def _artifact_name(self, *, kind: str, source_filename: str) -> str:
        source_path = Path(source_filename or 'document.hwpx')
        stem = source_path.stem or 'document'
        suffix = source_path.suffix or '.hwpx'
        if kind == 'screenshot':
            return f'{stem}-screenshot.png'
        if kind == 'export':
            return f'{stem}.pdf'
        if kind in {'working-copy', 'working_copy'}:
            return f'{stem}-edited{suffix}'
        if kind == 'recovery':
            return f'{stem}-recovered{suffix}'
        raise LocalCliServiceError(f'Unsupported local CLI artifact kind: {kind}', status_code=400)

    def _artifact_download_path(self, *, session_id: str, kind: str) -> str:
        return f'/local-cli/session/{session_id}/artifact/{kind}'

    def _public_artifacts(
        self,
        *,
        session_id: str,
        artifacts: dict[str, Any] | None,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Expose download routes and custody metadata, never server paths."""

        source = artifacts if isinstance(artifacts, dict) else {}
        public: dict[str, Any] = {}
        route_kinds = {
            'latest_working_copy_path': 'working-copy',
            'latest_export_path': 'export',
            'latest_screenshot_path': 'screenshot',
            'latest_recovery_path': 'recovery',
        }
        for key, kind in route_kinds.items():
            path = source.get(key)
            if (
                isinstance(path, str)
                and path
                and (
                    binding is None
                    or self._artifact_projection_is_available(binding=binding, kind=kind, path=Path(path))
                )
            ):
                public[key.replace('_path', '_download_path')] = self._artifact_download_path(
                    session_id=session_id,
                    kind=kind,
                )
        for key in ('latest_recovery_sha256', 'latest_recovery_size_bytes'):
            value = source.get(key)
            if value not in (None, ''):
                public[key] = value
        return public

    def public_artifact_projection(
        self,
        *,
        session_id: str,
        session: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Return routes proven by the current managed session binding.

        Interactive session metadata is caller-controlled state.  The public
        projection therefore reads both server-owned binding projections,
        requires them to identify the same session and generation, and lets
        the existing custody/readback checks decide which artifact kinds are
        downloadable.
        """

        resolved_session_id = str(session_id or '').strip()
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', resolved_session_id) is None:
            return {}
        if isinstance(session, dict):
            record_session_id = str(session.get('session_id') or '').strip()
            if record_session_id != resolved_session_id:
                return {}
            record_state = str(session.get('state') or '').strip().casefold()
            if record_state in {'closed', 'closed_cleanup_pending'}:
                return {}
            source_path = session.get('source_path')
        else:
            source_path = None

        try:
            binding = self._read_binding(session_id=resolved_session_id)
            active_binding = self._read_binding()
            if not isinstance(binding, dict) or not isinstance(active_binding, dict):
                return {}
            if self._binding_session_id(binding) != resolved_session_id:
                return {}
            if self._binding_session_id(active_binding) != resolved_session_id:
                return {}
            if binding != active_binding:
                return {}
            if self._is_session_closed(resolved_session_id):
                return {}
            binding_state = str(binding.get('document_session_state') or '').strip().casefold()
            if binding_state in {'closed', 'closed_cleanup_pending', 'stale'}:
                return {}
            if source_path:
                working_copy_path = str(binding.get('working_copy_path') or '').strip()
                if not working_copy_path:
                    return {}
                source_lexical = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(source_path))))
                working_lexical = os.path.normcase(os.path.normpath(os.path.abspath(working_copy_path)))
                if source_lexical != working_lexical:
                    return {}
            artifacts = binding.get('artifacts') if isinstance(binding.get('artifacts'), dict) else {}
            authoritative_artifacts = dict(artifacts)
            if 'latest_working_copy_path' not in authoritative_artifacts:
                working_copy_path = binding.get('working_copy_path')
                if isinstance(working_copy_path, str) and working_copy_path:
                    authoritative_artifacts['latest_working_copy_path'] = working_copy_path
            projected = self._public_artifacts(
                session_id=resolved_session_id,
                artifacts=authoritative_artifacts,
                binding=binding,
            )
            return {
                key: value
                for key, value in projected.items()
                if key.endswith('_download_path') and isinstance(value, str)
            }
        except (LocalCliServiceError, OSError, TypeError, ValueError):
            # Public status must fail closed when the binding is malformed or
            # disappears during reconciliation; it must never fall back to
            # caller-provided metadata.
            return {}

    def _validated_artifact_projection(
        self,
        *,
        binding: dict[str, Any],
        artifacts: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Keep only artifact paths proven to be children of this session root."""

        source = artifacts if isinstance(artifacts, dict) else {}
        projection: dict[str, Any] = {}
        path_kinds = {
            'latest_working_copy_path': 'working-copy',
            'latest_export_path': 'export',
            'latest_screenshot_path': 'screenshot',
            'latest_recovery_path': 'recovery',
        }
        for key, kind in path_kinds.items():
            value = source.get(key)
            if value in (None, ''):
                continue
            if not isinstance(value, str):
                raise LocalCliServiceError('Local CLI artifact path is invalid.', status_code=409)
            custody = {}
            projection[key] = str(self._verify_artifact_readback(binding, Path(value), readback=custody))
            custody_map = binding.get('artifact_custody') if isinstance(binding.get('artifact_custody'), dict) else {}
            prior = custody_map.get(kind) if isinstance(custody_map.get(kind), dict) else {}
            custody_map[kind] = {**prior, **custody}
            binding['artifact_custody'] = custody_map
        for key in ('latest_recovery_sha256', 'latest_recovery_size_bytes'):
            if source.get(key) not in (None, ''):
                projection[key] = source[key]
        return projection

    def _public_bundle_steps(
        self,
        *,
        session_id: str,
        steps: Any,
        binding: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Remove internal artifact paths from bounded bundle step results."""

        if not isinstance(steps, list):
            return []
        public_steps: list[dict[str, Any]] = []
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                continue
            step = _bounded_journal_value(raw_step)
            if not isinstance(step, dict):
                continue
            raw_result = raw_step.get('result')
            if isinstance(raw_result, dict):
                artifact_path = raw_result.get('artifact_path')
                artifact_kind = raw_result.get('artifact_kind')
                result = _bounded_journal_value(raw_result)
                if not isinstance(result, dict):
                    result = {}
                result.pop('artifact_path', None)
                if isinstance(artifact_path, str) and artifact_path:
                    if not isinstance(artifact_kind, str) or artifact_kind not in _RECOVERY_ARTIFACT_KINDS:
                        artifact_kind = _artifact_kind_from_key(str(step.get('op') or ''))
                    if (
                        artifact_kind in _RECOVERY_ARTIFACT_KINDS
                        and binding is not None
                        and self._artifact_projection_is_available(
                            binding=binding,
                            kind=artifact_kind,
                            path=Path(artifact_path),
                        )
                    ):
                        result['download_path'] = self._artifact_download_path(
                            session_id=session_id,
                            kind=artifact_kind,
                        )
                if isinstance(result.get('artifacts'), dict):
                    result['artifacts'] = self._public_artifacts(
                        session_id=session_id,
                        artifacts=result['artifacts'],
                        binding=binding,
                    )
                step['result'] = result
            if 'error' in step:
                step['error'] = 'native command step failed'
            public_steps.append(step)
        return public_steps

    def _artifact_projection_is_available(self, *, binding: dict[str, Any], kind: str, path: Path) -> bool:
        custody_map = binding.get('artifact_custody') if isinstance(binding.get('artifact_custody'), dict) else {}
        expected = custody_map.get(kind)
        if not isinstance(expected, dict):
            return False
        expected_size = expected.get('size_bytes')
        expected_sha256 = expected.get('sha256')
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None
        ):
            return False
        try:
            self._verify_artifact_readback(binding, path, expected=expected)
        except LocalCliServiceError:
            return False
        return True

    def _validated_artifact_path(self, binding: dict[str, Any], path: Path) -> tuple[Path, Path]:
        root_path = self._binding_session_root(binding)
        expected_root_identity = binding.get('session_root_identity')
        actual_root_identity = self._managed_path_identity(root_path)
        if actual_root_identity is None:
            raise LocalCliServiceError('Local CLI session root identity could not be verified.', status_code=409)
        if not isinstance(expected_root_identity, dict):
            raise LocalCliServiceError('Local CLI session root identity is missing; refusing artifact.', status_code=409)
        if actual_root_identity != expected_root_identity:
            raise LocalCliServiceError('Managed local CLI session root identity changed; refusing artifact.', status_code=409)
        if not stat.S_ISDIR(actual_root_identity.get('mode', 0)):
            raise LocalCliServiceError('Managed local CLI session root is not a directory.', status_code=409)
        if self._path_has_symlink_component(root_path):
            raise LocalCliServiceError('Local CLI session root path is symlinked.', status_code=409)
        try:
            root = root_path.resolve(strict=True)
            managed_root = self.sessions_root.resolve(strict=True)
            root.relative_to(managed_root)
        except (OSError, ValueError) as exc:
            raise LocalCliServiceError('Local CLI session root is outside the server session store.', status_code=409) from exc
        session_id = self._binding_session_id(binding)
        if root.parent != managed_root or root.name != session_id:
            raise LocalCliServiceError('Local CLI session root is not a managed session child.', status_code=409)
        if any(part in {'.', '..'} for part in path.parts):
            raise LocalCliServiceError('Local CLI artifact path contains traversal components.', status_code=409)
        lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
        try:
            relative = lexical.relative_to(root)
        except ValueError as exc:
            raise LocalCliServiceError('Local CLI artifact path is outside the managed session root.', status_code=409) from exc
        if not relative.parts:
            raise LocalCliServiceError('Local CLI artifact path is not a file.', status_code=409)
        current = root
        for part in relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise LocalCliServiceError('Local CLI artifact path is symlinked.', status_code=409)
            except OSError as exc:
                raise LocalCliServiceError('Local CLI artifact path could not be inspected.', status_code=409) from exc
        try:
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise LocalCliServiceError('Local CLI artifact is not available.', status_code=404) from exc
        if resolved != lexical or not resolved.is_file():
            raise LocalCliServiceError('Local CLI artifact is not a regular managed file.', status_code=409)
        identity = self._managed_path_identity(resolved)
        if identity is None or not stat.S_ISREG(identity.get('mode', 0)):
            raise LocalCliServiceError('Local CLI artifact is not a regular managed file.', status_code=409)
        return root, resolved

    @staticmethod
    def _identity_from_stat_result(stat_result: os.stat_result) -> dict[str, int]:
        return {
            'device': int(stat_result.st_dev),
            'inode': int(stat_result.st_ino),
            'mode': int(stat_result.st_mode),
        }

    def _open_artifact_fd(self, *, binding: dict[str, Any], root: Path, resolved: Path) -> int:
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
        nofollow = getattr(os, 'O_NOFOLLOW', 0)
        if os.name != 'nt' and getattr(os, 'O_DIRECTORY', 0) and os.open in os.supports_dir_fd:
            directory_fd: int | None = None
            artifact_fd: int | None = None
            try:
                directory_fd = os.open(
                    os.fspath(root),
                    flags | getattr(os, 'O_DIRECTORY', 0) | nofollow,
                )
                expected_root_identity = binding.get('session_root_identity')
                root_identity = self._identity_from_stat_result(os.fstat(directory_fd))
                if root_identity != expected_root_identity:
                    raise LocalCliServiceError('Managed local CLI session root identity changed before artifact open.', status_code=409)
                parts = resolved.relative_to(root).parts
                if not parts:
                    raise LocalCliServiceError('Local CLI artifact path is not a file.', status_code=409)
                for part in parts[:-1]:
                    next_fd = os.open(
                        part,
                        flags | getattr(os, 'O_DIRECTORY', 0) | nofollow,
                        dir_fd=directory_fd,
                    )
                    os.close(directory_fd)
                    directory_fd = next_fd
                artifact_fd = os.open(parts[-1], flags | nofollow, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = None
                result_fd = artifact_fd
                artifact_fd = None
                return result_fd
            except LocalCliServiceError:
                raise
            except (OSError, ValueError) as exc:
                raise LocalCliServiceError('Local CLI artifact could not be opened safely.', status_code=409) from exc
            finally:
                if directory_fd is not None:
                    try:
                        os.close(directory_fd)
                    except OSError:
                        pass
                if artifact_fd is not None:
                    try:
                        os.close(artifact_fd)
                    except OSError:
                        pass
        try:
            return os.open(os.fspath(resolved), flags | nofollow)
        except OSError as exc:
            raise LocalCliServiceError('Local CLI artifact could not be opened safely.', status_code=409) from exc

    def _open_verified_artifact(
        self,
        binding: dict[str, Any],
        path: Path,
        *,
        expected: dict[str, Any] | None = None,
        readback: dict[str, Any] | None = None,
    ) -> LocalCliArtifactDownload:
        root, resolved = self._validated_artifact_path(binding, path)
        expected_size = expected.get('size_bytes') if isinstance(expected, dict) else None
        expected_sha256 = expected.get('sha256') if isinstance(expected, dict) else None
        if expected is not None:
            if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
                raise LocalCliServiceError('Local CLI artifact custody size is invalid.', status_code=409)
            if not isinstance(expected_sha256, str) or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None:
                raise LocalCliServiceError('Local CLI artifact custody hash is invalid.', status_code=409)
        fd = self._open_artifact_fd(binding=binding, root=root, resolved=resolved)
        stream = None
        try:
            stream = os.fdopen(fd, 'rb')
            fd = -1
            identity_before = self._identity_from_stat_result(os.fstat(stream.fileno()))
            if not stat.S_ISREG(identity_before.get('mode', 0)):
                raise LocalCliServiceError('Local CLI artifact is not a regular managed file.', status_code=409)
            digest = hashlib.sha256()
            actual_size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                actual_size += len(chunk)
                digest.update(chunk)
            actual_sha256 = digest.hexdigest()
            if self._identity_from_stat_result(os.fstat(stream.fileno())) != identity_before:
                raise LocalCliServiceError('Local CLI artifact changed during custody readback.', status_code=409)
            if self._managed_path_identity(self._binding_session_root(binding)) != binding.get('session_root_identity'):
                raise LocalCliServiceError('Managed local CLI session root identity changed during artifact readback.', status_code=409)
            if expected is not None and (actual_size != expected_size or actual_sha256 != expected_sha256):
                raise LocalCliServiceError('Local CLI artifact changed after custody.', status_code=409)
            if readback is not None:
                readback.update({'size_bytes': actual_size, 'sha256': actual_sha256})
            stream.seek(0)
            return LocalCliArtifactDownload(
                stream=stream,
                path=resolved,
                size_bytes=actual_size,
                sha256=actual_sha256,
            )
        except Exception:
            if stream is not None:
                stream.close()
            elif fd >= 0:
                os.close(fd)
            raise

    def _verify_artifact_readback(
        self,
        binding: dict[str, Any],
        path: Path,
        *,
        expected: dict[str, Any] | None = None,
        readback: dict[str, Any] | None = None,
    ) -> Path:
        download = self._open_verified_artifact(binding, path, expected=expected, readback=readback)
        try:
            return download.path
        finally:
            download.close()

    def _artifact_spec(self, *, kind: str, session_id: str | None = None) -> tuple[dict[str, Any], Path, dict[str, Any] | None]:
        binding = self._load_active_binding(session_id=session_id, require_live=False)
        normalized_kind = 'working-copy' if kind == 'working_copy' else kind
        if normalized_kind == 'working-copy':
            path = self._working_copy_path(binding)
        elif normalized_kind in {'export', 'screenshot', 'recovery'}:
            artifacts = binding.get('artifacts') if isinstance(binding.get('artifacts'), dict) else {}
            path = Path(str(artifacts.get(f'latest_{normalized_kind}_path') or ''))
        else:
            raise LocalCliServiceError(f'Unsupported local CLI artifact kind: {kind}', status_code=400)
        custody_map = binding.get('artifact_custody') if isinstance(binding.get('artifact_custody'), dict) else {}
        expected = custody_map.get(normalized_kind) if isinstance(custody_map.get(normalized_kind), dict) else None
        return binding, path, expected

    def _resolve_artifact(self, *, kind: str, session_id: str | None = None) -> tuple[dict[str, Any], Path]:
        binding, path, expected = self._artifact_spec(kind=kind, session_id=session_id)
        try:
            path = self._verify_artifact_readback(binding, path, expected=expected)
        except LocalCliServiceError as exc:
            if exc.status_code == 404 and kind not in {'working-copy', 'working_copy'}:
                raise LocalCliServiceError(f'No local CLI {kind} artifact is available yet.', status_code=404) from exc
            raise
        return binding, path

    def open_artifact(self, *, kind: str, session_id: str | None = None) -> LocalCliArtifactDownload:
        binding, path, expected = self._artifact_spec(kind=kind, session_id=session_id)
        if not isinstance(expected, dict):
            raise LocalCliServiceError('Local CLI artifact custody is not available; refusing download.', status_code=409)
        filename = self._artifact_name(
            kind=kind,
            source_filename=str(binding.get('source_filename') or 'document.hwpx'),
        )
        download = self._open_verified_artifact(binding, path, expected=expected)
        download.filename = filename
        return download

    def _resolve_live_target(self, binding: dict[str, Any], target: str) -> tuple[str, int]:
        last_find = binding.get('last_find') if isinstance(binding.get('last_find'), dict) else {}
        cached_matches = last_find.get('matches') if isinstance(last_find.get('matches'), list) else []
        try:
            raw_target, match_number = resolve_match_target(target, cached_matches=cached_matches)
        except LocalCliDocumentError as exc:
            raise LocalCliServiceError(str(exc), status_code=400) from exc

        if match_number is None:
            return raw_target, 1

        query = str(last_find.get('query') or '').strip()
        if not query:
            raise LocalCliServiceError('No cached match list is available. Run hwpx find first or use text.', status_code=404)
        if cached_matches and match_number > len(cached_matches):
            raise LocalCliServiceError(f'No cached match number {match_number}.', status_code=404)

        # Prefer the concrete cached paragraph text over the original find query.
        # `find` uses normalized live text records, while live Hancom find can fail
        # on long/segmented queries. The cached paragraph gives `select 1` / `select 2`
        # a shorter, context-specific search/proof target instead of replaying
        # the same brittle long query against the native finder.
        if cached_matches:
            cached_match = cached_matches[match_number - 1]
            cached_text = str(cached_match.get('text') or '').strip() if isinstance(cached_match, dict) else ''
            if cached_text:
                normalized_cached_text = _normalize_visible_text(cached_text).casefold()
                duplicate_occurrence = 1
                for prior_match in cached_matches[: match_number - 1]:
                    if not isinstance(prior_match, dict):
                        continue
                    prior_text = str(prior_match.get('text') or '').strip()
                    if prior_text and _normalize_visible_text(prior_text).casefold() == normalized_cached_text:
                        duplicate_occurrence += 1
                return cached_text, duplicate_occurrence

        return query, match_number

    def _restore_selected_range(self, hwp: Any, selected_pos: Any) -> None:
        if isinstance(selected_pos, list):
            selected_pos = tuple(selected_pos)
        if isinstance(selected_pos, tuple) and selected_pos and selected_pos[0]:
            try:
                _select_text(hwp, selected_pos)
            except Exception:
                pass

    def _selected_ranges_equal(self, left: Any, right: Any) -> bool:
        if not (isinstance(left, (list, tuple)) and isinstance(right, (list, tuple))):
            return False
        if len(left) < 7 or len(right) < 7:
            return False
        try:
            return tuple(left[:7]) == tuple(right[:7])
        except Exception:
            return False

    def _verify_select_live_selection(
        self,
        hwp: Any,
        *,
        selected_range: Any,
        selected_text: str,
        query: str,
        match_safe_for_type: bool,
    ) -> dict[str, Any]:
        """Verify that a direct `select` result is still an active Hancom selection.

        `get_selected_text(keep_select=True)` and nearby-context probes can collapse
        Hancom's live selection on some pyhwpx builds.  For public `hwpx select`
        success, the trust basis is therefore the live `get_selected_pos()` range,
        restored when possible and checked immediately before returning.
        """

        expected_range = self._normalize_selected_range(selected_range)
        expected_range_list = list(expected_range) if expected_range is not None else None
        selected_text_value = str(selected_text or '')
        selected_text_normalized = _normalize_visible_text(selected_text_value)
        selected_text_available = bool(selected_text_normalized)
        selected_text_matches_query = bool(
            selected_text_available and _selected_text_contains_probe_relaxed(selected_text_value, query)
        )
        warnings: list[str] = []
        restore_attempted = False
        restore_error = None

        before_restore = _snapshot_cursor_context(hwp)
        final_snapshot = before_restore
        if expected_range is not None and not self._selected_ranges_equal(expected_range, before_restore.get('selected_pos')):
            restore_attempted = True
            try:
                _select_text(hwp, expected_range)
            except Exception as exc:  # pragma: no cover - live Hancom behavior is runtime-specific.
                restore_error = f'{type(exc).__name__}: {exc}'
            final_snapshot = _snapshot_cursor_context(hwp)
        else:
            final_snapshot = before_restore

        active_selection_verified = bool(
            expected_range is not None
            and final_snapshot.get('has_selection')
            and self._selected_ranges_equal(expected_range, final_snapshot.get('selected_pos'))
        )
        target_text_verified = bool(match_safe_for_type and selected_text_matches_query)
        safe_for_type = bool(match_safe_for_type and active_selection_verified and target_text_verified)

        degraded_reason = None
        selection_status = 'active'
        if not active_selection_verified:
            selection_status = 'degraded'
            if expected_range is None:
                degraded_reason = 'no restorable selected_pos was produced for the matched target'
            elif restore_error:
                degraded_reason = f'live Hancom selection could not be restored: {restore_error}'
            else:
                degraded_reason = 'live Hancom get_selected_pos did not match the selected range after verification'
        elif not selected_text_available:
            selection_status = 'degraded'
            degraded_reason = 'selected-text proof was empty; refusing to present the selection as reusable'
        elif not match_safe_for_type:
            selection_status = 'active-unsafe'
            degraded_reason = 'live selection is active, but this match strategy is an anchor/location proof only and is not safe for hwpx type'
        elif not target_text_verified:
            selection_status = 'degraded'
            degraded_reason = 'selected-text proof did not verify the requested target text'

        if restore_attempted and active_selection_verified:
            warnings.append('select verification restored the saved selected range before returning.')
        if degraded_reason:
            warnings.append(degraded_reason)

        return {
            'schema_version': 'local-cli/select-live-selection/v1',
            'selection_status': selection_status,
            'active_selection_verified': active_selection_verified,
            'safe_for_type': safe_for_type,
            'match_safe_for_type': bool(match_safe_for_type),
            'selected_text_available': selected_text_available,
            'selected_text_matches_query': selected_text_matches_query,
            'target_text_verified': target_text_verified,
            'expected_selected_range': expected_range_list,
            'snapshot_before_restore': before_restore,
            'snapshot_final': final_snapshot,
            'restore_attempted': restore_attempted,
            'restore_error': restore_error,
            'degraded_reason': degraded_reason,
            'warnings': warnings,
            'proof_method': 'live get_selected_pos verification with select_text(range) restore when needed; selected text captured before final live-position check',
        }

    def _cached_selection_proof(self, selection_cache: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(selection_cache, Mapping):
            return {}
        last_selection = selection_cache.get('last_selection')
        if not isinstance(last_selection, Mapping):
            last_selection = {}

        snapshot = last_selection.get('snapshot') if isinstance(last_selection.get('snapshot'), Mapping) else {}
        candidates = (
            selection_cache.get('selected_range'),
            last_selection.get('selected_range'),
            snapshot.get('selected_pos') if isinstance(snapshot, Mapping) else None,
        )
        selected_range = None
        for candidate in candidates:
            normalized = self._normalize_selected_range(candidate)
            if normalized is not None:
                selected_range = list(normalized)
                break

        selected_text = str(last_selection.get('selected_text') or '')
        selected_text_hash = str(last_selection.get('selected_text_hash') or '')
        if selected_text and not selected_text_hash:
            selected_text_hash = self._text_proof_hash(selected_text)
        return {
            'selected_range': selected_range,
            'selected_text': selected_text,
            'selected_text_normalized': _normalize_visible_text(selected_text),
            'selected_text_hash': selected_text_hash or None,
            'proof_source': last_selection.get('proof_source'),
        }

    def _capture_selected_text_proof_for_bundle(
        self,
        hwp: Any,
        *,
        keep_select: bool,
        selection_cache: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        before = _snapshot_cursor_context(hwp)
        before_range = before.get('selected_pos')
        had_selection = bool(before.get('has_selection'))
        had_active_selection_before_restore = had_selection
        cache = self._cached_selection_proof(selection_cache)
        cached_range = cache.get('selected_range')
        cached_text = str(cache.get('selected_text') or '')
        cached_text_normalized = str(cache.get('selected_text_normalized') or '')
        cached_text_hash = str(cache.get('selected_text_hash') or '')
        cached_selection_available = bool(cached_range)
        used_cached_selection = False
        selected_text_verified_against_cache = False
        warnings: list[str] = []

        if keep_select and not had_selection and cached_range:
            self._restore_selected_range(hwp, cached_range)
            restored_before = _snapshot_cursor_context(hwp)
            if not self._selected_ranges_equal(cached_range, restored_before.get('selected_pos')):
                raise LocalCliRuntimeError(
                    'selected-text proof could not restore the cached selected range; refusing to read fallback text.'
                )
            before = restored_before
            before_range = restored_before.get('selected_pos')
            had_selection = True
            used_cached_selection = True
            warnings.append('no active selection before selected-text proof; restored cached selected range from the active binding.')

        if keep_select and not had_selection:
            raise LocalCliRuntimeError(
                'selected-text proof requires an active selection or cached selected range; refusing to read fallback text.'
            )

        selected_text = _get_selected_text(hwp, keep_select=keep_select)
        selected_text_normalized = _normalize_visible_text(selected_text)
        selected_text_hash = self._text_proof_hash(selected_text)
        if not selected_text_normalized:
            raise LocalCliRuntimeError('selected-text proof captured empty text; refusing to report a reusable selection proof.')
        if cached_text_normalized:
            if selected_text_normalized != cached_text_normalized:
                raise LocalCliRuntimeError(
                    'selected-text proof mismatch against cached selected text; refusing to report a stale or widened selection proof.'
                )
            selected_text_verified_against_cache = True
        if cached_text_hash:
            if selected_text_hash != cached_text_hash:
                raise LocalCliRuntimeError(
                    'selected-text proof hash mismatch against cached selected proof; refusing to report a stale or widened selection proof.'
                )
            selected_text_verified_against_cache = True

        after_read = _snapshot_cursor_context(hwp)
        restored = after_read
        selection_restored = False

        if keep_select and had_selection:
            if not self._selected_ranges_equal(before_range, after_read.get('selected_pos')):
                self._restore_selected_range(hwp, before_range)
                restored = _snapshot_cursor_context(hwp)
                selection_restored = True
                if not self._selected_ranges_equal(before_range, restored.get('selected_pos')):
                    raise LocalCliRuntimeError(
                        'selected-text proof could not restore the pre-proof selection; refusing to report a reusable selection proof.'
                    )
                warnings.append('selected-text read changed the active selection; restored the saved selected range before returning proof.')
        elif had_selection and not keep_select:
            warnings.append('selection preservation was explicitly disabled by keep_select=false / --clear-selection.')

        if cached_text_normalized and selected_text_verified_against_cache:
            warnings.append('selected-text proof matched the cached selected text/hash from the active binding.')

        effective = restored if keep_select else after_read
        return {
            'schema_version': 'local-cli/selected-text-proof/v1',
            'proof_method': 'restore_cached_selected_range+pyhwpx.get_selected_text(keep_select=True)+verify_cached_text_hash' if used_cached_selection else ('pyhwpx.get_selected_text(keep_select=True)+restore_selected_range' if keep_select else 'pyhwpx.get_selected_text(keep_select=False)'),
            'keep_select_requested': bool(keep_select),
            'text_len': len(selected_text),
            'text_preview': _preview_text(selected_text, limit=160),
            'text_hash': selected_text_hash,
            'selected_text': selected_text,
            'selected_text_normalized': selected_text_normalized,
            'selection_source': 'cached-selection-restore' if used_cached_selection else ('active-selection' if had_selection else 'no-active-selection'),
            'used_active_selection': bool(had_selection and not used_cached_selection),
            'used_cached_selection': used_cached_selection,
            'cached_selection_available': cached_selection_available,
            'cached_selected_text_hash': cached_text_hash or None,
            'cached_selected_text_preview': _preview_text(cached_text, limit=160) if cached_text else None,
            'selected_text_verified_against_cache': selected_text_verified_against_cache,
            'selected_range_before': before_range,
            'selected_range_after_read': after_read.get('selected_pos'),
            'selected_range_restored': restored.get('selected_pos'),
            'has_active_selection_before': had_selection,
            'had_active_selection_before_restore': had_active_selection_before_restore,
            'has_active_selection_after_read': bool(after_read.get('has_selection')),
            'has_active_selection_restored': bool(restored.get('has_selection')),
            'selection_preserved_after_read': self._selected_ranges_equal(before_range, after_read.get('selected_pos')) if had_selection else False,
            'selection_restored': selection_restored,
            'snapshot_before': before,
            'snapshot_after_read': after_read,
            'snapshot_restored': restored,
            'snapshot': effective,
            'warnings': warnings,
            'fail_closed_conditions': [
                'missing active selection and missing cached selected range',
                'restore failure when keep_select=true and an active pre-proof selection existed',
                'cached selected text/hash mismatch when cached proof is available',
                'empty selected text for selection-required mutations',
            ],
        }

    def _selection_touches_url_boundary(self, *, paragraph_text: str, selected_range: Any, selected_text: str) -> bool:
        if not paragraph_text or not selected_text:
            return False
        if not (isinstance(selected_range, (list, tuple)) and len(selected_range) >= 7 and selected_range[0]):
            return False
        try:
            start_list, start_para, start_offset = int(selected_range[1]), int(selected_range[2]), int(selected_range[3])
            end_list, end_para, end_offset = int(selected_range[4]), int(selected_range[5]), int(selected_range[6])
        except Exception:
            return False
        if start_list != end_list or start_para != end_para:
            return False
        if start_offset < 0 or end_offset < start_offset:
            return False
        before = paragraph_text[max(0, start_offset - 80):start_offset]
        selected = paragraph_text[start_offset:end_offset] or selected_text
        after = paragraph_text[end_offset:end_offset + 80]
        token_chars = r'A-Za-z0-9:/._%#?=&+\-'
        left = re.search(f'[{token_chars}]*$', before)
        right = re.match(f'[{token_chars}]*', after)
        expanded = f'{left.group(0) if left else ""}{selected}{right.group(0) if right else ""}'
        endpoint_inside_token = bool(
            (before[-1:] and re.match(f'[{token_chars}]', before[-1]) and selected[:1] and re.match(f'[{token_chars}]', selected[0]))
            or (selected[-1:] and re.match(f'[{token_chars}]', selected[-1]) and after[:1] and re.match(f'[{token_chars}]', after[0]))
        )
        url_like = bool(re.search(r'(?i)(?:https?://|www\.|doi\.org/|\bdoi:\s*|\b10\.\d{4,9}/[-._;()/:A-Z0-9]+)', expanded))
        return bool(endpoint_inside_token and (url_like or '://' in expanded or '.' in expanded))

    def _capture_verified_live_match(
        self,
        hwp: Any,
        *,
        query: str,
        candidate: str,
    ) -> dict[str, Any] | None:
        snapshot = _snapshot_cursor_context(hwp)
        selected = _capture_selected_text_snapshot(hwp)
        selected_pos = snapshot.get('selected_pos')
        if _selected_text_contains_probe_relaxed(str(selected.get('selected_text') or ''), query):
            paragraph_text = ''
            try:
                paragraph_text = _get_current_paragraph_text_at_cursor(hwp)
            except Exception:
                paragraph_text = ''
            boundary_risk = self._selection_touches_url_boundary(
                paragraph_text=paragraph_text,
                selected_range=selected_pos,
                selected_text=str(selected.get('selected_text') or ''),
            )
            self._restore_selected_range(hwp, selected_pos)
            return {
                'query': query,
                'matched_query': candidate,
                'match_strategy': 'native-exact',
                'snapshot': snapshot,
                'paragraph_text_normalized': _normalize_visible_text(paragraph_text),
                'selected_text': selected.get('selected_text'),
                'selected_text_normalized': selected.get('selected_text_normalized'),
                'safe_for_type': not boundary_risk,
                'warning': 'Selection appears to start or end inside a URL/DOI-like token; active proof only, not safe for hwpx type.' if boundary_risk else None,
            }

        normalized_query = _normalize_visible_text(query)

        paragraph_text = ''
        try:
            paragraph_text = _get_current_paragraph_text_at_cursor(hwp)
        except Exception:
            paragraph_text = ''
        self._restore_selected_range(hwp, selected_pos)

        normalized_paragraph = _normalize_visible_text(paragraph_text)
        if normalized_query and _selected_text_contains_probe_relaxed(normalized_paragraph, normalized_query):
            try:
                _select_whole_paragraph_for_current_selection(hwp)
            except Exception:
                self._restore_selected_range(hwp, selected_pos)
            else:
                expanded_snapshot = _snapshot_cursor_context(hwp)
                expanded_selected = _capture_selected_text_snapshot(hwp)
                if _selected_text_contains_probe_relaxed(str(expanded_selected.get('selected_text') or ''), query):
                    expanded_pos = expanded_snapshot.get('selected_pos')
                    expanded_text_normalized = str(expanded_selected.get('selected_text_normalized') or '')
                    paragraph_exact_for_type = bool(expanded_text_normalized and expanded_text_normalized == normalized_query)
                    self._restore_selected_range(hwp, expanded_pos)
                    return {
                        'query': query,
                        'matched_query': candidate,
                        'match_strategy': 'paragraph-context-fallback',
                        'snapshot': expanded_snapshot,
                        'paragraph_text_normalized': normalized_paragraph,
                        'selected_text': expanded_selected.get('selected_text'),
                        'selected_text_normalized': expanded_selected.get('selected_text_normalized'),
                        'context_proof': {
                            'paragraph_text_normalized': normalized_paragraph,
                        },
                        'safe_for_type': paragraph_exact_for_type,
                        'warning': None if paragraph_exact_for_type else 'Paragraph fallback selected a wider live range than the query; selection is active proof only and not safe for hwpx type.',
                    }

        if normalized_query and _live_candidate_is_query_anchor(query=query, candidate=candidate):
            self._restore_selected_range(hwp, selected_pos)
            return {
                'query': query,
                'matched_query': candidate,
                'match_strategy': 'anchor-fallback',
                'snapshot': snapshot,
                'paragraph_text_normalized': normalized_paragraph,
                'selected_text': selected.get('selected_text'),
                'selected_text_normalized': selected.get('selected_text_normalized'),
                'safe_for_type': False,
                'warning': 'Only a native-searchable anchor was selected for this target; do not use hwpx type on this selection.',
            }

        return None


    def _live_find_candidates(self, query: str) -> list[tuple[str, bool]]:
        compact = ' '.join(str(query or '').split()).strip()
        candidates: list[tuple[str, bool]] = []
        if len(compact) >= 40:
            head = compact.split(':', 1)[0].strip()
            if len(head) >= 4:
                candidates.append((head, False))
            for marker in (':', '.', ' '):
                prefix = compact.split(marker, 1)[0].strip() if marker in compact else ''
                if len(prefix) >= 8:
                    candidates.append((prefix, False))
            for length in (24, 32, 48, 64):
                if len(compact) >= length:
                    prefix = compact[:length].strip()
                    if prefix:
                        candidates.append((prefix, False))
        candidates.extend(_build_live_heading_candidates(query))
        candidates.extend(_build_find_candidates(query))
        deduped: list[tuple[str, bool]] = []
        seen: set[tuple[str, bool]] = set()
        for candidate, allow_whole_word in candidates:
            key = (candidate, allow_whole_word)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    def _live_match_matches_target(
        self,
        match: Mapping[str, Any],
        *,
        target_identity: Mapping[str, Any] | None,
    ) -> bool:
        """Require live cursor evidence to identify the selected paragraph."""

        if not target_identity:
            return True
        raw_snapshot = match.get('snapshot')
        snapshot = raw_snapshot if isinstance(raw_snapshot, Mapping) else {}
        observed_pos = snapshot.get('pos')
        expected_pos = target_identity.get('live_position') or target_identity.get('position')
        if isinstance(expected_pos, (list, tuple)) and len(expected_pos) >= 2:
            if not isinstance(observed_pos, (list, tuple)) or len(observed_pos) < 2:
                return False
            try:
                if (int(observed_pos[0]), int(observed_pos[1])) != (int(expected_pos[0]), int(expected_pos[1])):
                    return False
            except (TypeError, ValueError):
                return False
        else:
            # A paragraph hash is not an occurrence identity.  In particular,
            # identical paragraphs at different positions must not allow the
            # first native match to satisfy a later static proof target.
            return False

        expected_hash = str(
            target_identity.get('paragraph_normalized_hash')
            or target_identity.get('normalized_hash')
            or ''
        ).strip().lower()
        if not expected_hash:
            return isinstance(expected_pos, (list, tuple)) and len(expected_pos) >= 2
        paragraph_text = str(match.get('paragraph_text_normalized') or '').strip()
        observed_hash = ''
        if paragraph_text:
            observed_hash = 'sha256:' + hashlib.sha256(paragraph_text.casefold().encode('utf-8')).hexdigest()
        if observed_hash:
            return observed_hash == expected_hash
        # A stable live position is required even when paragraph text cannot
        # be read back; the position is the occurrence binding.
        return isinstance(expected_pos, (list, tuple)) and len(expected_pos) >= 2

    def _find_live_match(
        self,
        hwp: Any,
        *,
        query: str,
        occurrence: int,
        target_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise LocalCliServiceError('target must not be empty', status_code=400)
        if occurrence <= 0:
            raise LocalCliServiceError('match number must be 1 or greater', status_code=400)

        find_method = getattr(hwp, 'find', None)
        if not callable(find_method):
            raise LocalCliServiceError('pyhwpx find is unavailable on this machine', status_code=500)

        for candidate, _allow_whole_word in self._live_find_candidates(query):
            _move_doc_begin(hwp)
            count = 0
            while find_method(candidate, direction='Forward', MatchCase=1, WholeWordOnly=0):
                match = self._capture_verified_live_match(hwp, query=query, candidate=candidate)
                if match is not None:
                    if not self._live_match_matches_target(match, target_identity=target_identity):
                        _move_after_selection(hwp)
                        continue
                    count += 1
                    if count == occurrence:
                        match['occurrence'] = occurrence
                        return match
                _move_after_selection(hwp)

        raise LocalCliServiceError(f'No match found for: {query}', status_code=404)

    def _table_context_from_live_snapshot(self, snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(snapshot, Mapping):
            return {'inside_table': False, 'table': {}}
        cell_ref = snapshot.get('cell_ref') if isinstance(snapshot.get('cell_ref'), Mapping) else {}
        cell_addr = str(snapshot.get('cell_addr') or cell_ref.get('addr') or '').strip().upper()
        inside_table = bool(snapshot.get('is_cell') is True or cell_addr)
        if not inside_table:
            return {'inside_table': False, 'table': {}}
        table = {
            'cell_addr': cell_addr or None,
            'row_1based': cell_ref.get('row_1based'),
            'col_1based': cell_ref.get('col_1based'),
            'row_index': cell_ref.get('row_index'),
            'col_index': cell_ref.get('col_index'),
            'source': 'live_cursor_snapshot',
        }
        return {'inside_table': True, 'table': {key: value for key, value in table.items() if value not in (None, '')}}

    def _enrich_live_find_matches_with_cursor_context(
        self,
        hwp: Any,
        *,
        query: str,
        matches: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not matches:
            return matches, []
        original_pos: tuple[int, int, int] | None = None
        warnings: list[str] = []
        try:
            original = _get_pos(hwp)
            if len(original) >= 3:
                original_pos = (int(original[0]), int(original[1]), int(original[2]))
        except Exception as exc:
            warnings.append(f'live find table-context proof could not save original caret position: {type(exc).__name__}: {exc}')

        enriched: list[dict[str, Any]] = []
        try:
            for fallback_occurrence, raw_match in enumerate(matches, start=1):
                match = dict(raw_match)
                try:
                    occurrence = int(match.get('number') or match.get('match_index') or fallback_occurrence)
                except Exception:
                    occurrence = fallback_occurrence
                try:
                    live_match = self._find_live_match(hwp, query=query, occurrence=occurrence)
                except Exception as exc:
                    match_warnings = list(match.get('warnings') or []) if isinstance(match.get('warnings'), list) else []
                    match_warnings.append(
                        f'live cursor table-context proof unavailable for occurrence {occurrence}: {type(exc).__name__}: {exc}'
                    )
                    match['warnings'] = match_warnings
                    enriched.append(match)
                    continue
                snapshot = live_match.get('snapshot') if isinstance(live_match.get('snapshot'), Mapping) else {}
                table_context = self._table_context_from_live_snapshot(snapshot)
                match['inside_table'] = bool(table_context.get('inside_table'))
                match['table'] = table_context.get('table') if table_context.get('inside_table') else {}
                identity = dict(match.get('identity') or {})
                if match.get('table'):
                    identity['table_cell_addr'] = match['table'].get('cell_addr')
                else:
                    identity['table_cell_addr'] = None
                live_pos = snapshot.get('pos')
                if isinstance(live_pos, (list, tuple)) and len(live_pos) >= 2:
                    identity['live_position'] = [live_pos[0], live_pos[1]]
                identity['paragraph_normalized_hash'] = match.get('normalized_hash')
                match['identity'] = identity
                match['live_cursor_proof'] = {
                    'occurrence': occurrence,
                    'matched_query': live_match.get('matched_query'),
                    'match_strategy': live_match.get('match_strategy'),
                    'pos': snapshot.get('pos'),
                    'selected_pos': snapshot.get('selected_pos'),
                    'is_cell': snapshot.get('is_cell'),
                    'cell_addr': snapshot.get('cell_addr'),
                    'selected_text_preview': _preview_text(live_match.get('selected_text'), limit=120),
                    'selection_restored_to_original_caret': original_pos is not None,
                }
                match_warnings = list(match.get('warnings') or []) if isinstance(match.get('warnings'), list) else []
                match_warnings.append('inside_table/table evidence verified from a live Hancom cursor snapshot; caret restored after proof.')
                match['warnings'] = match_warnings
                enriched.append(match)
        finally:
            if original_pos is not None:
                try:
                    _set_pos(hwp, original_pos[0], original_pos[1], original_pos[2])
                except Exception as exc:
                    warnings.append(f'live find table-context proof could not restore original caret position: {type(exc).__name__}: {exc}')
        return enriched, warnings

    def _run_caret_move(self, hwp: Any, *, direction: str, count: int) -> None:
        direction_key = str(direction or '').strip().lower()
        if direction_key not in {'left', 'right', 'up', 'down'}:
            raise LocalCliServiceError('cursormove direction must be one of: left, right, up, down', status_code=400)
        if count <= 0:
            raise LocalCliServiceError('cursormove count must be 1 or greater', status_code=400)

        method_name = {
            'left': 'MoveLeft',
            'right': 'MoveRight',
            'up': 'MoveUp',
            'down': 'MoveDown',
        }[direction_key]

        for _ in range(count):
            moved = False
            method = getattr(hwp, method_name, None)
            if callable(method):
                raw = method()
                moved = raw is None or bool(raw)
            if not moved:
                run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                if callable(run):
                    raw = run(method_name)
                    moved = raw is None or bool(raw)
            if not moved:
                raise LocalCliServiceError(f'pyhwpx cursormove is unavailable for direction={direction_key}', status_code=500)

    def _require_caret_in_cell(self, hwp: Any) -> None:
        snapshot: dict[str, Any] = {}
        try:
            snapshot = _snapshot_cursor_context(hwp)
        except Exception:
            snapshot = {}
        if snapshot.get('is_cell') is True and snapshot.get('cell_addr'):
            return

        cur_field_state = getattr(hwp, 'CurFieldState', None)
        try:
            cur_field_state = cur_field_state() if callable(cur_field_state) else cur_field_state
        except Exception:
            pass
        # Some pyhwpx/Hancom builds report table-cell text mode as 17 rather
        # than the older 1 while get_cell_addr()/KeyIndicator still proves the
        # caret is in a cell.  Prefer the cursor snapshot proof above, and only
        # keep this scalar fallback for legacy runtimes.
        if cur_field_state == 1:
            return
        raise LocalCliServiceError(
            f'The caret is not inside a table cell; CurFieldState={cur_field_state!r}; snapshot={snapshot}',
            status_code=400,
        )

    def _select_current_cell(self, hwp: Any) -> None:
        self._require_caret_in_cell(hwp)
        try:
            _run_table_cell_action(hwp, 'block')
        except EditOperationError as exc:
            raise LocalCliServiceError(f'Failed to select the current table cell: {exc}', status_code=500) from exc

    def _run_cell_move(self, hwp: Any, *, direction: str, count: int) -> None:
        direction_key = str(direction or '').strip().lower()
        if direction_key not in {'left', 'right', 'up', 'down'}:
            raise LocalCliServiceError('cellmove direction must be one of: left, right, up, down', status_code=400)
        if count <= 0:
            raise LocalCliServiceError('cellmove count must be 1 or greater', status_code=400)

        self._select_current_cell(hwp)
        for _ in range(count):
            try:
                _run_table_cell_action(hwp, direction_key)
            except EditOperationError as exc:
                raise LocalCliServiceError(f'Failed to move the current cell selection: {exc}', status_code=500) from exc

    def _run_single_action(self, hwp: Any, *, actions: tuple[str, ...], methods: tuple[str, ...], error_message: str) -> None:
        run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        if callable(run):
            for action in actions:
                try:
                    raw = run(action)
                    if raw is None or bool(raw):
                        return
                except Exception:
                    continue
        for method_name in methods:
            method = getattr(hwp, method_name, None)
            if callable(method):
                try:
                    raw = method()
                    if raw is None or bool(raw):
                        return
                except Exception:
                    continue
        raise LocalCliServiceError(error_message, status_code=500)

    def _style_scope_label(self, snapshot: dict[str, Any]) -> str:
        return 'current selection' if bool(snapshot.get('has_selection')) else 'current caret position'

    def _style_value_label(self, value: float) -> str:
        numeric = float(value)
        if numeric.is_integer():
            return str(int(numeric))
        return f'{numeric:g}'

    def _paragraph_preview_hash(self, preview: Any) -> str | None:
        text = str(preview or '').strip()
        return self._text_proof_hash(text) if text else None

    def _build_font_size_proof(
        self,
        *,
        requested_size_pt: float,
        before: dict[str, Any],
        after: dict[str, Any],
        style_result: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        after_preview = str(context.get('current_paragraph_preview') or '').strip()
        size_pt = float(requested_size_pt)
        strategy = style_result.get('strategy') or 'apply_char_style(height_pt)'
        return {
            'operation': 'fontsize',
            'scope': self._style_scope_label(before),
            'before_has_selection': bool(before.get('has_selection')),
            'after_has_selection': bool(after.get('has_selection')),
            'before_selected_pos': before.get('selected_pos'),
            'after_selected_pos': after.get('selected_pos'),
            'caret_pos_before': before.get('pos'),
            'caret_pos_after': after.get('pos'),
            'style': {
                'requested_font_size_pt': size_pt,
                'applied_font_size_pt': size_pt,
                'applied_font_size_source': 'apply_char_style command result; not a rendered/read-back visual proof',
                'strategy': style_result.get('strategy'),
            },
            'method': strategy,
            'after_paragraph_preview': after_preview or None,
            'after_paragraph_hash': self._paragraph_preview_hash(after_preview),
            'after_paragraph_hash_scope': 'current_paragraph_preview' if after_preview else None,
            'selection_cache_cleared': True,
        }

    def _build_type_text_proof(
        self,
        *,
        inserted_text: str,
        before: dict[str, Any],
        after: dict[str, Any],
        mode: str,
        strategy: str | None,
        before_selected_text: str,
        context: dict[str, Any],
        restored_cached_selection: bool = False,
        native_undo_steps: int | None = None,
    ) -> dict[str, Any]:
        after_preview = str(context.get('current_paragraph_preview') or '').strip()
        replaced_text = str(before_selected_text or '') if mode == 'replace-selection' else ''
        replaced_known = bool(replaced_text)
        selected_text_source = None
        if replaced_known:
            selected_text_source = 'cached-selected-text-proof'
        elif mode == 'replace-selection':
            selected_text_source = 'not-read-before-type-to-preserve-selection'
        return {
            'operation': 'type',
            'scope': mode,
            'before_has_selection': bool(before.get('has_selection')),
            'after_has_selection': bool(after.get('has_selection')),
            'before_selected_pos': before.get('selected_pos'),
            'after_selected_pos': after.get('selected_pos'),
            'caret_pos_before': before.get('pos'),
            'caret_pos_after': after.get('pos'),
            'selected_text_preview': _preview_text(replaced_text, limit=80) if replaced_known else None,
            'selected_text_len': len(replaced_text) if replaced_known else None,
            'selected_text_source': selected_text_source,
            'replaced_text_preview': _preview_text(replaced_text, limit=80) if replaced_known else None,
            'replaced_text_len': len(replaced_text) if replaced_known else None,
            'replaced_text_hash': self._text_proof_hash(replaced_text) if replaced_known else None,
            'replaced_text_known': replaced_known,
            'inserted_text_len': len(inserted_text),
            'inserted_text_hash': self._text_proof_hash(inserted_text),
            'strategy': strategy,
            'method': strategy or ('Delete+insert_text' if mode == 'replace-selection' else 'insert_text_at_caret'),
            'native_undo_steps': native_undo_steps,
            'restored_cached_selection': bool(restored_cached_selection),
            'after_paragraph_preview': after_preview or None,
            'after_paragraph_hash': self._paragraph_preview_hash(after_preview),
            'after_paragraph_hash_scope': 'current_paragraph_preview' if after_preview else None,
            'selection_cache_cleared': True,
        }

    def _compact_state_payload(
        self,
        *,
        location: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = context if isinstance(context, dict) else {}
        current_preview = location.get('current_paragraph_preview') or context.get('current_paragraph_preview')
        return {
            'cursor_summary': location.get('cursor_summary'),
            'selection_summary': location.get('selection_summary'),
            'current_paragraph_preview': current_preview,
        }

    def _apply_bullet_to_current_paragraph(self, hwp: Any) -> None:
        _select_whole_paragraph_for_current_selection(hwp)
        self._run_single_action(
            hwp,
            actions=('PutBullet',),
            methods=('PutBullet',),
            error_message='pyhwpx PutBullet is unavailable on this machine',
        )

    def _break_paragraph(self, hwp: Any) -> None:
        self._run_single_action(
            hwp,
            actions=('BreakPara',),
            methods=('BreakPara',),
            error_message='Failed to create a new paragraph.',
        )

    def _validate_macro_path(self, method_path: str) -> tuple[str, list[str]]:
        path = str(method_path or '').strip()
        if not path:
            raise LocalCliServiceError('pycall method_path must not be empty', status_code=400)
        if '__' in path:
            raise LocalCliServiceError('pycall method_path must not contain dunder/private access', status_code=400)
        segments = path.split('.')
        if len(segments) > _MACRO_MAX_PATH_SEGMENTS:
            raise LocalCliServiceError('pycall method_path is too deep', status_code=400)
        for segment in segments:
            if not segment or segment.startswith('_') or '__' in segment:
                raise LocalCliServiceError('pycall method_path may only use public attributes', status_code=400)
            if not segment.isidentifier():
                raise LocalCliServiceError('pycall method_path segments must be Python identifiers', status_code=400)
        return path, segments

    def _validate_macro_json_value(self, value: Any, *, field_name: str, depth: int = 0) -> Any:
        if depth > _MACRO_MAX_JSON_DEPTH:
            raise LocalCliServiceError(f'{field_name} is nested too deeply', status_code=400)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) > _MACRO_MAX_STRING_CHARS:
                raise LocalCliServiceError(f'{field_name} string value is too long', status_code=400)
            return value
        if isinstance(value, list):
            if len(value) > _MACRO_MAX_ARGS:
                raise LocalCliServiceError(f'{field_name} list has too many items', status_code=400)
            return [self._validate_macro_json_value(item, field_name=field_name, depth=depth + 1) for item in value]
        if isinstance(value, dict):
            if len(value) > _MACRO_MAX_KWARGS:
                raise LocalCliServiceError(f'{field_name} object has too many keys', status_code=400)
            cleaned: dict[str, Any] = {}
            for raw_key, raw_item in value.items():
                if not isinstance(raw_key, str) or not raw_key:
                    raise LocalCliServiceError(f'{field_name} object keys must be non-empty strings', status_code=400)
                if raw_key.startswith('_') or '__' in raw_key:
                    raise LocalCliServiceError(f'{field_name} object keys may not be private/dunder names', status_code=400)
                cleaned[raw_key] = self._validate_macro_json_value(raw_item, field_name=field_name, depth=depth + 1)
            return cleaned
        raise LocalCliServiceError(f'{field_name} must be JSON-compatible', status_code=400)

    def _validate_macro_args(self, args: list[Any], kwargs: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        if not isinstance(args, list):
            raise LocalCliServiceError('pycall args must be a JSON array', status_code=400)
        if not isinstance(kwargs, dict):
            raise LocalCliServiceError('pycall kwargs must be a JSON object', status_code=400)
        if len(args) > _MACRO_MAX_ARGS:
            raise LocalCliServiceError('pycall args has too many items', status_code=400)
        if len(kwargs) > _MACRO_MAX_KWARGS:
            raise LocalCliServiceError('pycall kwargs has too many keys', status_code=400)
        return (
            [self._validate_macro_json_value(item, field_name='pycall args') for item in args],
            self._validate_macro_json_value(kwargs, field_name='pycall kwargs'),
        )

    def _resolve_public_macro_leaf(self, root: Any, segments: list[str]) -> Any:
        current = root
        traversed: list[str] = []
        for segment in segments:
            traversed.append(segment)
            try:
                current = getattr(current, segment)
            except Exception as exc:
                dotted = '.'.join(traversed)
                raise LocalCliRuntimeError(f'pycall path is not available: {dotted}') from exc
        return current

    def _macro_result_preview(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 3:
            return '<max-depth>'
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) <= _MACRO_PREVIEW_STRING_CHARS:
                return value
            return f'{value[:_MACRO_PREVIEW_STRING_CHARS]}...'
        if isinstance(value, (list, tuple)):
            items = [self._macro_result_preview(item, depth=depth + 1) for item in list(value)[:_MACRO_PREVIEW_ITEMS]]
            if len(value) > _MACRO_PREVIEW_ITEMS:
                items.append(f'<{len(value) - _MACRO_PREVIEW_ITEMS} more>')
            return items
        if isinstance(value, dict):
            preview: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= _MACRO_PREVIEW_ITEMS:
                    preview['<more>'] = len(value) - _MACRO_PREVIEW_ITEMS
                    break
                preview[str(key)] = self._macro_result_preview(item, depth=depth + 1)
            return preview
        try:
            raw = repr(value)
        except Exception:
            raw = f'<{type(value).__name__}>'
        if len(raw) > _MACRO_PREVIEW_STRING_CHARS:
            raw = f'{raw[:_MACRO_PREVIEW_STRING_CHARS]}...'
        return raw

    def _validate_action_name(self, action_name: str) -> str:
        action = str(action_name or '').strip()
        if not action:
            raise LocalCliServiceError('action_name must not be empty', status_code=400)
        if action.startswith('_') or '__' in action:
            raise LocalCliServiceError('action_name may not be private/dunder', status_code=400)
        if len(action) > 100:
            raise LocalCliServiceError('action_name is too long', status_code=400)
        return action

    def _validate_command_bundle_steps(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(steps, list):
            raise LocalCliServiceError('command-bundle steps must be a JSON array', status_code=400)
        if not steps:
            raise LocalCliServiceError('command-bundle steps must not be empty', status_code=400)
        if len(steps) > _BUNDLE_MAX_STEPS:
            raise LocalCliServiceError(f'command-bundle accepts at most {_BUNDLE_MAX_STEPS} steps', status_code=400)

        allowed_keys: dict[str, set[str]] = {
            'context': {'op', 'operation', 'label'},
            'selection_proof': {'op', 'operation', 'label'},
            'control_inventory': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'max_controls',
            },
            'table_frame_inventory': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'max_controls',
            },

            'selected_text_delete_exact': {
                'op',
                'operation',
                'label',
                'expected_text',
                'expected_hash',
                'expected_normalized_hash',
                'confirm_cleanup',
                'source_text_deleted',
                'native_table_proof_ref',
                'native_table_proof_hash',
            },
            'anchor_range_replace_native_table': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'start_anchor',
                'end_before_anchor',
                'required_source_basename',
                'forbid_source_basename',
                'expected_range_hash',
                'expected_normalized_range_hash',
                'caption_text',
                'rows',
                'cols',
                'cells',
                'field_name',
                'confirm_replace',
                'flat_value_hashes',
                'non_empty_token_count',
                'non_empty_token_preview',
                'warnings',
                'next_proof_required',
            },
            'table_cell_structure_exact': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'max_controls',
            },
            'table_column_width_exact': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'expected_preimage_sha256',
                'expected_cell_inventory_hash',
                'expected_document_text_hash',
                'expected_text_char_count',
                'expected_nonempty_line_count',
                'expected_div0_count',
                'expected_rows',
                'expected_cols',
                'expected_total_width_mm',
                'expected_table_height_mm',
                'expected_control_count',
                'expected_bindata_manifest_hash',
                'requested_widths_mm',
                'confirm_layout',
                'max_controls',
            },
            'export_pdf': {'op', 'operation', 'label'},
            'hwp_action': {'op', 'operation', 'label', 'action_name'},
            'pyhwpx_call': {'op', 'operation', 'label', 'method_path', 'args', 'kwargs'},
            'save_document': {'op', 'operation', 'label'},
            'set_text_file': {'op', 'operation', 'label', 'text', 'format', 'option'},
            'anchor_insert': {'op', 'operation', 'label', 'target', 'position', 'text', 'fragments'},
            'get_selected_text': {'op', 'operation', 'label', 'keep_select'},
            'style_inspect': {'op', 'operation', 'label', 'match', 'keep_position'},
            'paragraph_style_apply_exact': {
                'op',
                'operation',
                'label',
                'match',
                'expected_page',
                'keep_with_next',
                'widow_orphan',
                'pagebreak_before',
                'confirm_layout',
            },
            'paragraph_delete_exact': {
                'op',
                'operation',
                'label',
                'match',
                'expected_page',
                'occurrence_on_page',
                'expected_previous_contains',
                'expected_next_contains',
                'confirm_remove',
                'max_page_after',
            },
            'paragraph_join_previous_exact': {
                'op',
                'operation',
                'label',
                'match',
                'expected_page',
                'expected_previous_contains',
                'delete_back_count',
                'max_page_after',
                'confirm_layout',
            },
            'paragraph_join_next_exact': {
                'op',
                'operation',
                'label',
                'match',
                'expected_page',
                'expected_next_contains',
                'delete_count',
                'next_match',
                'max_next_page_after',
                'insert_line_break',
                'move_to_line_end',
                'confirm_layout',
            },
            'control_join_previous_exact': {
                'op',
                'operation',
                'label',
                'target_id',
                'expected_hash',
                'expected_page',
                'page_from',
                'page_to',
                'max_controls',
                'delete_back_count',
                'max_page_after',
                'confirm_layout',
            },
            'paragraph_rehome_exact': {
                'op',
                'operation',
                'label',
                'delete_match',
                'delete_expected_page',
                'insert_before_match',
                'insert_expected_page',
                'insert_text',
                'expected_text_delta',
                'confirm_layout',
            },
            'control_delete_exact': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'confirm_remove',
                'max_controls',
            },
            'exact_control_select_proof': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'max_controls',
            },
            'cell_format_exact': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'cell_margin_hu',
                'cell_margin_mm',
                'vertical_align',
                'fill_color',
                'border',
                'confirm_layout',
                'max_controls',
            },
            'cell_row_fit_exact': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'row_height_percent',
                'row_height_hu',
                'row_height_mm',
                'resize_up_steps',
                'resize_down_steps',
                'line_spacing',
                'char_height_percent',
                'confirm_layout',
                'max_controls',
            },
            'native_table_insert': {
                'op',
                'operation',
                'label',
                'rows',
                'cols',
                'cells',
                'field_name',
                'confirm_native_table',
                'source_text_deleted',
                'old_plain_text_removal',
                'flat_value_hashes',
                'non_empty_token_count',
                'non_empty_token_preview',
                'warnings',
                'next_proof_required',
                'split_by_column',
                'split_column_index',
                'split_group_value',
                'split_group_index',
                'split_group_count',
                'split_group_hash',
            },
            'table_split_exact': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'down_rows',
                'confirm_layout',
                'max_controls',
            },
            'control_move_resize_exact': {
                'op',
                'operation',
                'label',
                'section_anchor',
                'page_from',
                'page_to',
                'around',
                'target_id',
                'expected_hash',
                'expected_page',
                'scale_percent',
                'move_dx_mm',
                'move_dy_mm',
                'confirm_layout',
                'max_controls',
            },
            'where': {'op', 'operation', 'label'},
            'readback': {
                'op',
                'operation',
                'label',
                'scope',
                'page_from',
                'page_to',
                'max_blocks',
                'max_table_cells',
                'max_controls',
            },
            'typography_overview': {'op', 'operation', 'label', 'scope', 'max_samples', 'max_sections', 'max_styles'},
        }

        cleaned: list[dict[str, Any]] = []
        for index, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict):
                raise LocalCliServiceError(f'command-bundle step {index} must be an object', status_code=400)
            step = dict(raw_step)
            op = str(step.get('op') or step.get('operation') or '').strip()
            if op not in _BUNDLE_ALLOWED_OPS:
                supported = ', '.join(sorted(_BUNDLE_ALLOWED_OPS))
                raise LocalCliServiceError(f'command-bundle step {index} has unsupported op={op!r}. Supported: {supported}', status_code=400)
            package_allowed_keys = self.command_packages.allowed_keys(op)
            step_allowed_keys = package_allowed_keys or allowed_keys[op]
            unknown = sorted(set(step) - step_allowed_keys)
            if unknown:
                raise LocalCliServiceError(
                    f'command-bundle step {index} op={op!r} has unsupported fields: {", ".join(unknown)}',
                    status_code=400,
                )
            step['op'] = op
            label = str(step.get('label') or f'step-{index}:{op}').strip()
            if not label or len(label) > 80:
                raise LocalCliServiceError(f'command-bundle step {index} label must be 1-80 characters', status_code=400)
            step['label'] = label

            package = self.command_packages.get(op)
            if package is not None:
                step = package.validate(service=self, index=index, step=step, error_type=LocalCliServiceError)
            elif op == 'hwp_action':
                action = self._validate_action_name(str(step.get('action_name') or ''))
                if action not in _BUNDLE_SAFE_HACTION_NAMES:
                    safe = ', '.join(sorted(_BUNDLE_SAFE_HACTION_NAMES))
                    raise LocalCliServiceError(f'command-bundle step {index} hwp_action {action!r} is not allowed. Safe actions: {safe}', status_code=400)
                step['action_name'] = action
            elif op == 'pyhwpx_call':
                path, _segments = self._validate_macro_path(str(step.get('method_path') or ''))
                if path not in _BUNDLE_SAFE_PYHWPX_CALLS:
                    safe = ', '.join(sorted(_BUNDLE_SAFE_PYHWPX_CALLS))
                    raise LocalCliServiceError(f'command-bundle step {index} pyhwpx_call {path!r} is not allowed. Safe paths: {safe}', status_code=400)
                cleaned_args, cleaned_kwargs = self._validate_macro_args(step.get('args') or [], step.get('kwargs') or {})
                step['method_path'] = path
                step['args'] = cleaned_args
                step['kwargs'] = cleaned_kwargs
            elif op == 'save_document':
                pass
            elif op == 'set_text_file':
                text = step.get('text')
                if not isinstance(text, str) or not text:
                    raise LocalCliServiceError(f'command-bundle step {index} set_text_file requires non-empty text', status_code=400)
                if len(text) > _MACRO_MAX_STRING_CHARS:
                    raise LocalCliServiceError(f'command-bundle step {index} set_text_file text is too long', status_code=400)
                fmt = str(step.get('format') or 'UNICODE').strip().upper()
                option = str(step.get('option') or 'insertfile').strip().lower()
                if fmt != 'UNICODE' or option != 'insertfile':
                    raise LocalCliServiceError(
                        f'command-bundle step {index} set_text_file only supports format=UNICODE and option=insertfile',
                        status_code=400,
                    )
                step['format'] = fmt
                step['option'] = option
            elif op == 'anchor_insert':
                target = str(step.get('target') or '').strip()
                if not target:
                    raise LocalCliServiceError(f'command-bundle step {index} anchor_insert requires non-empty target', status_code=400)
                if len(target) > 500:
                    raise LocalCliServiceError(f'command-bundle step {index} anchor_insert target is too long', status_code=400)
                position = self._normalize_anchor_insert_position(step.get('position'))
                text = step.get('text')
                fragments = step.get('fragments')
                if text is not None and fragments is not None:
                    raise LocalCliServiceError(f'command-bundle step {index} anchor_insert accepts text or fragments, not both', status_code=400)
                if fragments is not None:
                    if not isinstance(fragments, list) or not fragments or any(not isinstance(item, str) or item == '' for item in fragments):
                        raise LocalCliServiceError(f'command-bundle step {index} anchor_insert fragments must be non-empty strings', status_code=400)
                    if sum(len(item) for item in fragments) > _MACRO_MAX_STRING_CHARS:
                        raise LocalCliServiceError(f'command-bundle step {index} anchor_insert fragments are too long', status_code=400)
                    step['fragments'] = fragments
                elif not isinstance(text, str) or text == '':
                    raise LocalCliServiceError(f'command-bundle step {index} anchor_insert requires non-empty text or fragments', status_code=400)
                elif len(text) > _MACRO_MAX_STRING_CHARS:
                    raise LocalCliServiceError(f'command-bundle step {index} anchor_insert text is too long', status_code=400)
                step['target'] = target
                step['position'] = position
            elif op == 'get_selected_text':
                if 'keep_select' in step and not isinstance(step.get('keep_select'), bool):
                    raise LocalCliServiceError(f'command-bundle step {index} keep_select must be boolean when provided', status_code=400)
            elif op == 'style_inspect':
                if 'match' in step and step.get('match') not in (None, ''):
                    value = str(step.get('match') or '').strip()
                    if len(value) > 500:
                        raise LocalCliServiceError(f'command-bundle step {index} match is too long', status_code=400)
                    step['match'] = value
                elif 'match' in step:
                    step['match'] = None
                if 'keep_position' in step and not isinstance(step.get('keep_position'), bool):
                    raise LocalCliServiceError(f'command-bundle step {index} keep_position must be boolean when provided', status_code=400)
            elif op == 'paragraph_style_apply_exact':
                value = str(step.get('match') or '').strip()
                if not value:
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_style_apply_exact requires match', status_code=400)
                if len(value) > 500:
                    raise LocalCliServiceError(f'command-bundle step {index} match is too long', status_code=400)
                step['match'] = value
                value = step.get('expected_page')
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_style_apply_exact requires positive integer expected_page', status_code=400)
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_style_apply_exact requires confirm_layout=true', status_code=400)
                for bool_key in ('keep_with_next', 'widow_orphan'):
                    if bool_key in step and step.get(bool_key) is not None and not isinstance(step.get(bool_key), bool):
                        raise LocalCliServiceError(f'command-bundle step {index} {bool_key} must be boolean', status_code=400)
                if 'pagebreak_before' in step and step.get('pagebreak_before') is not None:
                    pagebreak_before = step.get('pagebreak_before')
                    if isinstance(pagebreak_before, bool) or not isinstance(pagebreak_before, int) or int(pagebreak_before) not in (0, 1):
                        raise LocalCliServiceError(f'command-bundle step {index} pagebreak_before must be 0 or 1', status_code=400)
            elif op == 'paragraph_delete_exact':
                value = str(step.get('match') or '').strip()
                if not value:
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_delete_exact requires match', status_code=400)
                if len(value) > 500:
                    raise LocalCliServiceError(f'command-bundle step {index} match is too long', status_code=400)
                step['match'] = value
                expected_page = step.get('expected_page')
                if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page <= 0:
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_delete_exact requires positive integer expected_page', status_code=400)
                occurrence = int(step.get('occurrence_on_page') or 1)
                if not (1 <= occurrence <= 200):
                    raise LocalCliServiceError(f'command-bundle step {index} occurrence_on_page must be 1..200', status_code=400)
                step['occurrence_on_page'] = occurrence
                for guard_key in ('expected_previous_contains', 'expected_next_contains'):
                    if step.get(guard_key) not in (None, ''):
                        guard = str(step.get(guard_key) or '').strip()
                        if len(guard) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {guard_key} is too long', status_code=400)
                        step[guard_key] = guard
                    elif guard_key in step:
                        step[guard_key] = None
                max_page = step.get('max_page_after')
                if max_page not in (None, ''):
                    if isinstance(max_page, bool) or not isinstance(max_page, int) or max_page <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} max_page_after must be a positive integer', status_code=400)
                if step.get('confirm_remove') is not True:
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_delete_exact requires confirm_remove=true', status_code=400)
            elif op in {'paragraph_join_previous_exact', 'paragraph_join_next_exact'}:
                value = str(step.get('match') or '').strip()
                if not value:
                    raise LocalCliServiceError(f'command-bundle step {index} {op} requires match', status_code=400)
                if len(value) > 500:
                    raise LocalCliServiceError(f'command-bundle step {index} match is too long', status_code=400)
                step['match'] = value
                expected_page = step.get('expected_page')
                if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page <= 0:
                    raise LocalCliServiceError(f'command-bundle step {index} {op} requires positive integer expected_page', status_code=400)
                count_key = 'delete_back_count' if op == 'paragraph_join_previous_exact' else 'delete_count'
                count_value = int(step.get(count_key) or 1)
                if not (1 <= count_value <= 5):
                    raise LocalCliServiceError(f'command-bundle step {index} {count_key} must be 1..5', status_code=400)
                step[count_key] = count_value
                page_key = 'max_page_after' if op == 'paragraph_join_previous_exact' else 'max_next_page_after'
                max_page = step.get(page_key)
                if max_page not in (None, ''):
                    if isinstance(max_page, bool) or not isinstance(max_page, int) or max_page <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {page_key} must be a positive integer', status_code=400)
                guard_key = 'expected_previous_contains' if op == 'paragraph_join_previous_exact' else 'expected_next_contains'
                if step.get(guard_key) not in (None, ''):
                    guard = str(step.get(guard_key) or '').strip()
                    if len(guard) > 500:
                        raise LocalCliServiceError(f'command-bundle step {index} {guard_key} is too long', status_code=400)
                    step[guard_key] = guard
                elif guard_key in step:
                    step[guard_key] = None
                if op == 'paragraph_join_next_exact' and step.get('next_match') not in (None, ''):
                    nxt = str(step.get('next_match') or '').strip()
                    if len(nxt) > 500:
                        raise LocalCliServiceError(f'command-bundle step {index} next_match is too long', status_code=400)
                    step['next_match'] = nxt
                elif 'next_match' in step:
                    step['next_match'] = None
                if op == 'paragraph_join_next_exact' and 'insert_line_break' in step and not isinstance(step.get('insert_line_break'), bool):
                    raise LocalCliServiceError(f'command-bundle step {index} insert_line_break must be boolean', status_code=400)
                if op == 'paragraph_join_next_exact' and 'move_to_line_end' in step and not isinstance(step.get('move_to_line_end'), bool):
                    raise LocalCliServiceError(f'command-bundle step {index} move_to_line_end must be boolean', status_code=400)
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(f'command-bundle step {index} {op} requires confirm_layout=true', status_code=400)
            elif op in {'control_inventory', 'table_frame_inventory'}:
                for text_key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
                    if text_key in step and step.get(text_key) not in (None, ''):
                        value = str(step.get(text_key) or '').strip()
                        if len(value) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                        step[text_key] = value
                    elif text_key in step:
                        step[text_key] = None
                for int_key in ('page_from', 'page_to', 'expected_page', 'max_controls', 'resize_up_steps', 'resize_down_steps'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                page_from = step.get('page_from')
                page_to = step.get('page_to')
                if page_from is not None and page_to is not None and int(page_to) < int(page_from):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not step.get('section_anchor') and not step.get('page_from'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} {op} requires section_anchor or page_from',
                        status_code=400,
                    )
            elif op == 'control_join_previous_exact':
                for text_key in ('target_id', 'expected_hash'):
                    value = str(step.get(text_key) or '').strip()
                    if not value:
                        raise LocalCliServiceError(f'command-bundle step {index} control_join_previous_exact requires {text_key}', status_code=400)
                    if len(value) > 500:
                        raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                    step[text_key] = value
                for int_key in ('page_from', 'page_to', 'expected_page', 'max_controls', 'delete_back_count', 'max_page_after'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                if step.get('page_from') is not None and step.get('page_to') is not None and int(step.get('page_to')) < int(step.get('page_from')):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not str(step.get('target_id')).startswith('ctrl/'):
                    raise LocalCliServiceError(f'command-bundle step {index} control_join_previous_exact requires exact target_id from inventory', status_code=400)
                if not str(step.get('expected_hash')).startswith('sha256:'):
                    raise LocalCliServiceError(f'command-bundle step {index} control_join_previous_exact requires expected_hash from inventory', status_code=400)
                if not step.get('expected_page'):
                    raise LocalCliServiceError(f'command-bundle step {index} control_join_previous_exact requires expected_page', status_code=400)
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(f'command-bundle step {index} control_join_previous_exact requires confirm_layout=true', status_code=400)
                count_value = int(step.get('delete_back_count') or 1)
                if not (1 <= count_value <= 5):
                    raise LocalCliServiceError(f'command-bundle step {index} delete_back_count must be 1..5', status_code=400)
                step['delete_back_count'] = count_value
            elif op == 'paragraph_rehome_exact':
                for text_key in ('delete_match', 'insert_before_match', 'insert_text'):
                    value = str(step.get(text_key) or '')
                    if not value.strip():
                        raise LocalCliServiceError(f'command-bundle step {index} paragraph_rehome_exact requires {text_key}', status_code=400)
                    if len(value) > 1000:
                        raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                    step[text_key] = value
                for int_key in ('delete_expected_page', 'insert_expected_page'):
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                if step.get('expected_text_delta') not in (None, 0):
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_rehome_exact expected_text_delta must be 0', status_code=400)
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(f'command-bundle step {index} paragraph_rehome_exact requires confirm_layout=true', status_code=400)
            elif op == 'control_delete_exact':
                for text_key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
                    if text_key in step and step.get(text_key) not in (None, ''):
                        value = str(step.get(text_key) or '').strip()
                        if len(value) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                        step[text_key] = value
                    elif text_key in step:
                        step[text_key] = None
                for int_key in ('page_from', 'page_to', 'expected_page', 'max_controls'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                page_from = step.get('page_from')
                page_to = step.get('page_to')
                if page_from is not None and page_to is not None and int(page_to) < int(page_from):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not step.get('section_anchor') and not step.get('page_from'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_delete_exact requires section_anchor or page_from',
                        status_code=400,
                    )
                if not step.get('target_id') or not str(step.get('target_id')).startswith('ctrl/'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_delete_exact requires exact target_id from inventory',
                        status_code=400,
                    )
                if not step.get('expected_hash') or not str(step.get('expected_hash')).startswith('sha256:'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_delete_exact requires expected_hash from inventory',
                        status_code=400,
                    )
                if not step.get('expected_page'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_delete_exact requires expected_page',
                        status_code=400,
                    )
                if step.get('confirm_remove') is not True:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_delete_exact requires confirm_remove=true',
                        status_code=400,
                    )

            elif op in {'exact_control_select_proof', 'table_cell_structure_exact'}:
                for text_key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
                    if text_key in step and step.get(text_key) not in (None, ''):
                        value = str(step.get(text_key) or '').strip()
                        if len(value) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                        step[text_key] = value
                    elif text_key in step:
                        step[text_key] = None
                for int_key in ('page_from', 'page_to', 'expected_page', 'max_controls'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                page_from = step.get('page_from')
                page_to = step.get('page_to')
                if page_from is not None and page_to is not None and int(page_to) < int(page_from):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not step.get('section_anchor') and not step.get('page_from'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} {op} requires section_anchor or page_from',
                        status_code=400,
                    )
                if not step.get('target_id') or not str(step.get('target_id')).startswith('ctrl/'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} {op} requires exact target_id from inventory',
                        status_code=400,
                    )
                if not step.get('expected_hash') or not str(step.get('expected_hash')).startswith('sha256:'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} {op} requires expected_hash from inventory',
                        status_code=400,
                    )
                if not step.get('expected_page'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} {op} requires expected_page',
                        status_code=400,
                    )

            elif op == 'cell_format_exact':
                for text_key in ('section_anchor', 'around', 'target_id', 'expected_hash', 'vertical_align', 'fill_color', 'border'):
                    if text_key in step and step.get(text_key) not in (None, ''):
                        value = str(step.get(text_key) or '').strip()
                        if len(value) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                        step[text_key] = value
                    elif text_key in step:
                        step[text_key] = None
                for int_key in ('page_from', 'page_to', 'expected_page', 'max_controls'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                for number_key in ('cell_margin_hu', 'cell_margin_mm'):
                    if number_key not in step or step.get(number_key) in (None, ''):
                        continue
                    value = step.get(number_key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise LocalCliServiceError(f'command-bundle step {index} {number_key} must be numeric', status_code=400)
                    step[number_key] = float(value)
                page_from = step.get('page_from')
                page_to = step.get('page_to')
                if page_from is not None and page_to is not None and int(page_to) < int(page_from):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not step.get('section_anchor') and not step.get('page_from'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_format_exact requires section_anchor or page_from',
                        status_code=400,
                    )
                if not step.get('target_id') or not str(step.get('target_id')).startswith('ctrl/'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_format_exact requires exact target_id from inventory',
                        status_code=400,
                    )
                if not step.get('expected_hash') or not str(step.get('expected_hash')).startswith('sha256:'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_format_exact requires expected_hash from inventory',
                        status_code=400,
                    )
                if not step.get('expected_page'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_format_exact requires expected_page',
                        status_code=400,
                    )
                selectors = [key for key in ('cell_margin_hu', 'cell_margin_mm', 'vertical_align', 'fill_color', 'border') if step.get(key) is not None]
                if len(selectors) != 1:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_format_exact requires exactly one format selector',
                        status_code=400,
                    )
                if step.get('cell_margin_hu') is not None and not (0.0 <= float(step.get('cell_margin_hu')) <= 20000.0):
                    raise LocalCliServiceError(f'command-bundle step {index} cell_margin_hu out of safe range', status_code=400)
                if step.get('cell_margin_mm') is not None and not (0.0 <= float(step.get('cell_margin_mm')) <= 70.0):
                    raise LocalCliServiceError(f'command-bundle step {index} cell_margin_mm out of safe range', status_code=400)
                if step.get('vertical_align') is not None and step.get('vertical_align') not in {'top', 'center', 'middle', 'bottom'}:
                    raise LocalCliServiceError(f'command-bundle step {index} vertical_align must be top, center, middle, or bottom', status_code=400)
                if step.get('vertical_align') == 'middle':
                    step['vertical_align'] = 'center'
                if step.get('fill_color') is not None:
                    fill_color = str(step.get('fill_color')).upper()
                    if re.fullmatch(r'#[0-9A-F]{6}', fill_color) is None:
                        raise LocalCliServiceError(f'command-bundle step {index} fill_color must be #RRGGBB', status_code=400)
                    step['fill_color'] = fill_color
                if step.get('border') is not None and step.get('border') != 'none':
                    raise LocalCliServiceError(f'command-bundle step {index} border must be none', status_code=400)
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_format_exact requires confirm_layout=true',
                        status_code=400,
                    )


            elif op == 'table_split_exact':
                for text_key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
                    if text_key in step and step.get(text_key) not in (None, ''):
                        value = str(step.get(text_key) or '').strip()
                        if len(value) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                        step[text_key] = value
                    elif text_key in step:
                        step[text_key] = None
                for int_key in ('page_from', 'page_to', 'expected_page', 'down_rows', 'max_controls'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                page_from = step.get('page_from')
                page_to = step.get('page_to')
                if page_from is not None and page_to is not None and int(page_to) < int(page_from):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not step.get('section_anchor') and not step.get('page_from'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} table_split_exact requires section_anchor or page_from',
                        status_code=400,
                    )
                if not step.get('target_id') or not str(step.get('target_id')).startswith('ctrl/'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} table_split_exact requires exact target_id from inventory',
                        status_code=400,
                    )
                if not step.get('expected_hash') or not str(step.get('expected_hash')).startswith('sha256:'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} table_split_exact requires expected_hash from inventory',
                        status_code=400,
                    )
                if not step.get('expected_page'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} table_split_exact requires expected_page',
                        status_code=400,
                    )
                if not (1 <= int(step.get('down_rows') or 0) <= 200):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} table_split_exact requires down_rows 1..200',
                        status_code=400,
                    )
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} table_split_exact requires confirm_layout=true',
                        status_code=400,
                    )

            elif op == 'control_move_resize_exact':
                for text_key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
                    if text_key in step and step.get(text_key) not in (None, ''):
                        value = str(step.get(text_key) or '').strip()
                        if len(value) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                        step[text_key] = value
                    elif text_key in step:
                        step[text_key] = None
                for int_key in ('page_from', 'page_to', 'expected_page', 'max_controls'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                for number_key in ('scale_percent', 'move_dx_mm', 'move_dy_mm'):
                    if number_key not in step or step.get(number_key) in (None, ''):
                        continue
                    value = step.get(number_key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise LocalCliServiceError(f'command-bundle step {index} {number_key} must be numeric', status_code=400)
                    step[number_key] = float(value)
                page_from = step.get('page_from')
                page_to = step.get('page_to')
                if page_from is not None and page_to is not None and int(page_to) < int(page_from):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not step.get('section_anchor') and not step.get('page_from'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_move_resize_exact requires section_anchor or page_from',
                        status_code=400,
                    )
                if not step.get('target_id') or not str(step.get('target_id')).startswith('ctrl/'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_move_resize_exact requires exact target_id from inventory',
                        status_code=400,
                    )
                if not step.get('expected_hash') or not str(step.get('expected_hash')).startswith('sha256:'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_move_resize_exact requires expected_hash from inventory',
                        status_code=400,
                    )
                if not step.get('expected_page'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_move_resize_exact requires expected_page',
                        status_code=400,
                    )
                scale_percent = step.get('scale_percent')
                move_dx_mm = float(step.get('move_dx_mm') or 0.0)
                move_dy_mm = float(step.get('move_dy_mm') or 0.0)
                if scale_percent is None and move_dx_mm == 0.0 and move_dy_mm == 0.0:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_move_resize_exact requires scale_percent or non-zero move delta',
                        status_code=400,
                    )
                if scale_percent is not None and not (5.0 <= float(scale_percent) <= 200.0):
                    raise LocalCliServiceError(f'command-bundle step {index} scale_percent must be 5..200', status_code=400)
                if abs(move_dx_mm) > 300.0 or abs(move_dy_mm) > 300.0:
                    raise LocalCliServiceError(f'command-bundle step {index} move deltas must be within +/-300mm', status_code=400)
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} control_move_resize_exact requires confirm_layout=true',
                        status_code=400,
                    )

            elif op == 'cell_row_fit_exact':
                for text_key in ('section_anchor', 'around', 'target_id', 'expected_hash'):
                    if text_key in step and step.get(text_key) not in (None, ''):
                        value = str(step.get(text_key) or '').strip()
                        if len(value) > 500:
                            raise LocalCliServiceError(f'command-bundle step {index} {text_key} is too long', status_code=400)
                        step[text_key] = value
                    elif text_key in step:
                        step[text_key] = None
                for int_key in ('page_from', 'page_to', 'expected_page', 'max_controls'):
                    if int_key not in step or step.get(int_key) in (None, ''):
                        continue
                    value = step.get(int_key)
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise LocalCliServiceError(f'command-bundle step {index} {int_key} must be a positive integer', status_code=400)
                for number_key in ('row_height_percent', 'row_height_hu', 'row_height_mm', 'char_height_percent'):
                    if number_key not in step or step.get(number_key) in (None, ''):
                        continue
                    value = step.get(number_key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise LocalCliServiceError(f'command-bundle step {index} {number_key} must be numeric', status_code=400)
                    step[number_key] = float(value)
                page_from = step.get('page_from')
                page_to = step.get('page_to')
                if page_from is not None and page_to is not None and int(page_to) < int(page_from):
                    raise LocalCliServiceError(f'command-bundle step {index} page_to must be >= page_from', status_code=400)
                if not step.get('section_anchor') and not step.get('page_from'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_row_fit_exact requires section_anchor or page_from',
                        status_code=400,
                    )
                if not step.get('target_id') or not str(step.get('target_id')).startswith('ctrl/'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_row_fit_exact requires exact target_id from inventory',
                        status_code=400,
                    )
                if not step.get('expected_hash') or not str(step.get('expected_hash')).startswith('sha256:'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_row_fit_exact requires expected_hash from inventory',
                        status_code=400,
                    )
                if not step.get('expected_page'):
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_row_fit_exact requires expected_page',
                        status_code=400,
                    )
                selectors = [key for key in ('row_height_percent', 'row_height_hu', 'row_height_mm', 'resize_up_steps', 'resize_down_steps', 'line_spacing', 'char_height_percent') if step.get(key) is not None]
                if len(selectors) != 1:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_row_fit_exact requires exactly one row height selector',
                        status_code=400,
                    )
                if step.get('row_height_percent') is not None and not (20.0 <= float(step.get('row_height_percent')) <= 120.0):
                    raise LocalCliServiceError(f'command-bundle step {index} row_height_percent must be 20..120', status_code=400)
                if step.get('row_height_hu') is not None and not (1000.0 <= float(step.get('row_height_hu')) <= 200000.0):
                    raise LocalCliServiceError(f'command-bundle step {index} row_height_hu out of safe range', status_code=400)
                if step.get('row_height_mm') is not None and not (3.0 <= float(step.get('row_height_mm')) <= 700.0):
                    raise LocalCliServiceError(f'command-bundle step {index} row_height_mm out of safe range', status_code=400)
                for step_key in ('resize_up_steps', 'resize_down_steps'):
                    if step.get(step_key) is not None and not (1 <= int(step.get(step_key)) <= 200):
                        raise LocalCliServiceError(f'command-bundle step {index} {step_key} must be 1..200', status_code=400)
                if step.get('line_spacing') is not None:
                    value = step.get('line_spacing')
                    if isinstance(value, bool) or not isinstance(value, int) or not (80 <= value <= 200):
                        raise LocalCliServiceError(f'command-bundle step {index} line_spacing must be integer 80..200', status_code=400)
                if step.get('char_height_percent') is not None and not (70.0 <= float(step.get('char_height_percent')) <= 110.0):
                    raise LocalCliServiceError(f'command-bundle step {index} char_height_percent must be 70..110', status_code=400)
                if step.get('confirm_layout') is not True:
                    raise LocalCliServiceError(
                        f'command-bundle step {index} cell_row_fit_exact requires confirm_layout=true',
                        status_code=400,
                    )

            cleaned.append(step)
        return cleaned

    def _bundle_require_text(self, step: dict[str, Any], field_name: str, *, max_chars: int = _MACRO_MAX_STRING_CHARS) -> str:
        value = step.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise LocalCliRuntimeError(f'command-bundle {step.get("op")} requires non-empty {field_name}')
        if len(value) > max_chars:
            raise LocalCliRuntimeError(f'command-bundle {step.get("op")} {field_name} is too long')
        return value

    def _bundle_compact_snapshot(self, hwp: Any) -> dict[str, Any]:
        try:
            snapshot = _snapshot_cursor_context(hwp)
        except Exception:
            return {'error': 'native location snapshot unavailable'}
        return {
            'pos': snapshot.get('pos'),
            'cell_addr': snapshot.get('cell_addr'),
            'is_cell': snapshot.get('is_cell'),
            'has_selection': snapshot.get('has_selection'),
            'selection_mode': snapshot.get('selection_mode'),
            'current_paragraph_preview': None,
        }


    def _bundle_is_cell(self, hwp: Any) -> bool:
        is_cell_method = getattr(hwp, 'is_cell', None)
        if callable(is_cell_method):
            try:
                return bool(is_cell_method())
            except Exception:
                return False
        try:
            snapshot = self._bundle_compact_snapshot(hwp)
            return bool(snapshot.get('is_cell'))
        except Exception:
            return False

    def _bundle_current_cell_addr_tuple(self, hwp: Any) -> tuple[int, int] | None:
        getter = getattr(hwp, 'get_cell_addr', None)
        if not callable(getter):
            return None
        for args, kwargs in ((( ), {'as_': 'tuple'}), (( ), {'as_': 'str'}), (( ), {})):
            try:
                value = getter(*args, **kwargs)
            except Exception:
                continue
            if isinstance(value, tuple) and len(value) >= 2:
                try:
                    return (int(value[0]), int(value[1]))
                except Exception:
                    continue
            if isinstance(value, str):
                text = value.strip().upper()
                match = re.fullmatch(r'([A-Z]+)(\d+)', text)
                if match:
                    col = 0
                    for char in match.group(1):
                        col = col * 26 + (ord(char) - ord('A') + 1)
                    return (col - 1, int(match.group(2)) - 1)
        return None

    def _bundle_compact_location(self, location: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(location, dict):
            return {}
        return {
            'cursor_summary': location.get('cursor_summary'),
            'selection_summary': location.get('selection_summary'),
            'current_paragraph_preview': None,
            'caret_in_table_cell': location.get('caret_in_table_cell'),
            'document_is_modified': location.get('document_is_modified'),
        }

    def _bundle_page_evidence(self, hwp: Any) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        for name in ('current_page', 'get_current_page', 'GetCurrentPage'):
            method = getattr(hwp, name, None)
            if not callable(method):
                continue
            try:
                value = method()
                if isinstance(value, int) and value > 0:
                    return {'page': int(value), 'method': name}
            except Exception as exc:
                evidence[f'{name}_error'] = str(exc)
        for name in ('Page', 'CurrentPage'):
            try:
                value = getattr(hwp, name, None)
            except Exception as exc:
                evidence[f'{name}_error'] = str(exc)
                continue
            if isinstance(value, int) and value > 0:
                return {'page': int(value), 'method': name}
        key_indicator = getattr(hwp, 'KeyIndicator', None)
        if callable(key_indicator):
            try:
                raw = key_indicator()
                evidence['key_indicator_preview'] = self._macro_result_preview(raw)
                if isinstance(raw, (list, tuple)):
                    # pyhwpx/Hancom KeyIndicator returns several 1-based status
                    # values. In live checks, index 3 tracks the rendered page,
                    # while earlier integer fields can be constant status/list
                    # values. Prefer index 3 when present; fall back only after.
                    preferred_indexes = [3, 2, 1, 0]
                    for index in preferred_indexes:
                        if index >= len(raw):
                            continue
                        value = raw[index]
                        if isinstance(value, int) and value > 0:
                            return {'page': int(value), 'method': f'KeyIndicator[{index}]', 'raw_preview': evidence.get('key_indicator_preview')}
                        if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
                            return {'page': int(value.strip()), 'method': f'KeyIndicator[{index}]', 'raw_preview': evidence.get('key_indicator_preview')}
            except Exception as exc:
                evidence['KeyIndicator_error'] = str(exc)
        return {'page': None, 'method': None, **evidence}

    def _bundle_control_proof_item(self, hwp: Any, ctrl: Any, index: int) -> tuple[dict[str, Any], dict[str, Any], tuple[int, int, int] | None]:
        anchor_pos = _get_ctrl_anchor_pos(hwp, ctrl, option=1)
        snapshot: dict[str, Any] = {}
        page_evidence: dict[str, Any] = {'page': None, 'method': None}
        if anchor_pos is not None:
            _set_pos(hwp, anchor_pos[0], anchor_pos[1], anchor_pos[2])
            snapshot = _snapshot_cursor_context(hwp)
            page_evidence = self._bundle_page_evidence(hwp)
        page = page_evidence.get('page')
        ctrl_id = self._bundle_control_scalar(ctrl, 'CtrlID')
        ctrl_inst_id = self._bundle_control_scalar(ctrl, 'CtrlInstID')
        user_desc = self._bundle_control_scalar(ctrl, 'UserDesc')
        type_name = self._bundle_control_scalar(ctrl, 'Type') or self._bundle_control_scalar(ctrl, 'ShapeType')
        bounds = {
            key: self._bundle_control_scalar(ctrl, key)
            for key in ('X', 'Y', 'Width', 'Height', 'HorzRelTo', 'VertRelTo')
            if self._bundle_control_scalar(ctrl, key) not in (None, '')
        }
        proof_basis = {
            'index': index,
            'ctrl_id': ctrl_id,
            'ctrl_inst_id': ctrl_inst_id,
            'user_desc': user_desc,
            'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
            'bounds': bounds or None,
            'page': page,
            'type': type_name,
        }
        proof_hash = 'sha256:' + hashlib.sha256(json.dumps(proof_basis, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:24]
        stable_id = f"ctrl/{index}/{ctrl_id or 'unknown'}/{ctrl_inst_id or 'no-inst'}"
        item = {
            'target_id': stable_id,
            'index': index,
            'page': page,
            'page_evidence': page_evidence,
            'type': str(type_name or ctrl_id or 'control'),
            'ctrl_id': ctrl_id,
            'ctrl_inst_id': ctrl_inst_id,
            'bounds': bounds or None,
            'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
            'text_preview': _preview_text(str(user_desc or ''), limit=120) if user_desc else None,
            'proof_hash': proof_hash,
            'nearby': {
                'cell_addr': snapshot.get('cell_addr'),
                'field_name': snapshot.get('field_name'),
                'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
            },
        }
        return item, snapshot, anchor_pos

    def _bundle_find_anchor_evidence(self, hwp: Any, anchor: str | None) -> dict[str, Any] | None:
        if not anchor:
            return None
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            _move_doc_begin(hwp)
            found = False
            find_method = getattr(hwp, 'find', None)
            if callable(find_method):
                try:
                    found = bool(find_method(anchor, direction='Forward', MatchCase=1, WholeWordOnly=0))
                except TypeError:
                    found = bool(find_method(anchor))
            snapshot = _snapshot_cursor_context(hwp) if found else {}
            return {
                'anchor': anchor,
                'found': found,
                'pos': snapshot.get('pos'),
                'page': self._bundle_page_evidence(hwp).get('page') if found else None,
                'selection_mode': snapshot.get('selection_mode'),
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_control_scalar(self, ctrl: Any, attr: str) -> Any:
        method_fallbacks = {
            'CtrlInstID': ('GetCtrlInstID',),
        }
        try:
            value = getattr(ctrl, attr, None)
        except Exception:
            value = None
        if callable(value):
            value = None
        if value is None:
            for method_name in method_fallbacks.get(attr, ()):
                try:
                    method = getattr(ctrl, method_name, None)
                except Exception:
                    method = None
                if not callable(method):
                    continue
                try:
                    value = method()
                    break
                except Exception:
                    value = None
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def _capture_control_map_signature(self, hwp: Any, *, max_controls: int = 2048) -> dict[str, Any]:
        """Capture a bounded control count/type signature without moving the caret."""

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        type_sequence: list[str] = []
        identity_sequence: list[dict[str, str]] = []
        type_counts: dict[str, int] = {}
        for ctrl in controls:
            ctrl_id = str(self._bundle_control_scalar(ctrl, 'CtrlID') or '')
            ctrl_inst_id = str(self._bundle_control_scalar(ctrl, 'CtrlInstID') or '')
            type_name = str(
                self._bundle_control_scalar(ctrl, 'Type')
                or self._bundle_control_scalar(ctrl, 'ShapeType')
                or ctrl_id
                or 'control'
            )
            type_sequence.append(type_name)
            identity_sequence.append({'ctrl_id': ctrl_id, 'ctrl_inst_id': ctrl_inst_id, 'type': type_name})
            type_counts[type_name] = type_counts.get(type_name, 0) + 1
        sequence_payload = json.dumps(type_sequence, ensure_ascii=False, separators=(',', ':'))
        identity_payload = json.dumps(identity_sequence, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        return {
            'control_count': len(controls),
            'type_counts': dict(sorted(type_counts.items())),
            'type_sequence_sha256': hashlib.sha256(sequence_payload.encode('utf-8')).hexdigest(),
            'identity_sequence_sha256': hashlib.sha256(identity_payload.encode('utf-8')).hexdigest(),
            'enumeration_mode': enumeration_mode,
        }

    def _assert_control_map_unchanged(
        self,
        *,
        before: dict[str, Any],
        after: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        before_count = int(before.get('control_count') or 0)
        after_count = int(after.get('control_count') or 0)
        before_types = dict(before.get('type_counts') or {})
        after_types = dict(after.get('type_counts') or {})
        before_sequence = str(before.get('type_sequence_sha256') or '')
        after_sequence = str(after.get('type_sequence_sha256') or '')
        before_identity = str(before.get('identity_sequence_sha256') or '')
        after_identity = str(after.get('identity_sequence_sha256') or '')
        added_types = {
            key: int(after_types.get(key, 0)) - int(before_types.get(key, 0))
            for key in sorted(set(before_types) | set(after_types))
            if int(after_types.get(key, 0)) > int(before_types.get(key, 0))
        }
        removed_types = {
            key: int(before_types.get(key, 0)) - int(after_types.get(key, 0))
            for key in sorted(set(before_types) | set(after_types))
            if int(before_types.get(key, 0)) > int(after_types.get(key, 0))
        }
        passed = (
            before_count == after_count
            and before_types == after_types
            and before_sequence == after_sequence
            and before_identity == after_identity
        )
        proof = {
            'passed': passed,
            'operation': operation,
            'before_control_count': before_count,
            'after_control_count': after_count,
            'before_type_counts': before_types,
            'after_type_counts': after_types,
            'added_types': added_types,
            'removed_types': removed_types,
            'before_type_sequence_sha256': before_sequence,
            'after_type_sequence_sha256': after_sequence,
            'before_identity_sequence_sha256': before_identity,
            'after_identity_sequence_sha256': after_identity,
        }
        if not passed:
            raise LocalCliRuntimeError(
                'CONTROL_DRIFT: '
                f'{operation} changed the native control map '
                f'{before_count}->{after_count}; '
                f'added_types={json.dumps(added_types, ensure_ascii=False, sort_keys=True)}; '
                f'removed_types={json.dumps(removed_types, ensure_ascii=False, sort_keys=True)}; '
                f'identity_sha256={before_identity}->{after_identity}. '
                'The working copy was not saved; close without saving and retry with a structure-preserving primitive.'
            )
        return proof

    def _bundle_selected_control_summary(self, hwp: Any) -> dict[str, Any]:
        try:
            ctrl = getattr(hwp, 'CurSelectedCtrl', None)
        except Exception as exc:
            return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
        if ctrl is None:
            return {'available': False, 'control': None}
        summary = {
            'available': True,
            'ctrl_id': self._bundle_control_scalar(ctrl, 'CtrlID'),
            'ctrl_inst_id': self._bundle_control_scalar(ctrl, 'CtrlInstID'),
            'user_desc': self._bundle_control_scalar(ctrl, 'UserDesc'),
            'type': self._bundle_control_scalar(ctrl, 'Type') or self._bundle_control_scalar(ctrl, 'ShapeType'),
        }
        summary['shape_properties'] = self._shape_prop_snapshot(ctrl)
        return summary

    def _bundle_selected_control_matches_target(self, selected: dict[str, Any], target_item: dict[str, Any]) -> tuple[bool, str]:
        target_inst_id = str(target_item.get('ctrl_inst_id') or '').strip()
        selected_inst_id = str(selected.get('ctrl_inst_id') or '').strip()
        if target_inst_id and selected_inst_id:
            return selected_inst_id == target_inst_id, 'ctrl_inst_id'
        target_id = str(target_item.get('ctrl_id') or target_item.get('type') or '').strip()
        selected_id = str(selected.get('ctrl_id') or selected.get('type') or '').strip()
        if target_id and selected_id:
            return selected_id == target_id, 'ctrl_id-fallback'
        return False, 'unavailable'

    def _bundle_select_control_exact(self, hwp: Any, ctrl: Any, target_item: dict[str, Any]) -> dict[str, Any]:
        """Try documented pyhwpx control selection, preferring Hancom 2024 CtrlInstID.

        The returned proof is explicit about method strength.  Exact CtrlInstID
        selection is preferred, but older runtimes may only support weaker
        object/anchor fallback selection; those paths are reported instead of
        being hidden.
        """

        attempts: list[dict[str, Any]] = []
        target_inst_id = str(target_item.get('ctrl_inst_id') or self._bundle_control_scalar(ctrl, 'CtrlInstID') or '').strip()
        target_ctrl_id = str(target_item.get('ctrl_id') or '').strip()

        def _record_attempt(method: str, raw_result: Any = None, error: Exception | None = None, **extra: Any) -> dict[str, Any]:
            if error is not None:
                attempt = {'method': method, 'error': f'{type(error).__name__}: {error}', **extra}
                attempts.append(attempt)
                return attempt
            selected = self._bundle_selected_control_summary(hwp)
            matches, match_basis = self._bundle_selected_control_matches_target(selected, target_item)
            selected_available = bool(selected.get('available'))
            succeeded = matches or bool(raw_result) or (raw_result is None and selected_available)
            proof_strength = 'exact-ctrl-inst-id' if matches and match_basis == 'ctrl_inst_id' else ('fallback-selected-control' if matches else 'unverified-native-return')
            attempt = {
                'method': method,
                'result': bool(raw_result) if raw_result is not None else None,
                'selection_succeeded': succeeded,
                'selected_matches_target': matches,
                'match_basis': match_basis,
                'proof_strength': proof_strength,
                'selected_control': selected,
                **extra,
            }
            attempts.append(attempt)
            return attempt

        select_ctrl_exact = getattr(hwp, 'SelectCtrl', None)
        if target_inst_id and callable(select_ctrl_exact):
            for args in ((str(target_inst_id), 1), (str(target_inst_id),), (target_inst_id, 1), (target_inst_id,)):
                try:
                    raw = select_ctrl_exact(*args)
                    attempt = _record_attempt('SelectCtrl(GetCtrlInstID)', raw, ctrl_inst_id=target_inst_id, args=list(args))
                    if attempt.get('selected_matches_target'):
                        return {'selection_succeeded': True, 'method_used': attempt.get('method'), 'proof_strength': attempt.get('proof_strength'), 'target_ctrl_inst_id': target_inst_id, 'target_ctrl_id': target_ctrl_id, 'selected_control': attempt.get('selected_control'), 'attempts': attempts}
                except Exception as exc:
                    _record_attempt('SelectCtrl(GetCtrlInstID)', error=exc, ctrl_inst_id=target_inst_id, args=list(args))
        elif not target_inst_id:
            attempts.append({'method': 'SelectCtrl(GetCtrlInstID)', 'skipped': True, 'reason': 'target control has no CtrlInstID'})
        else:
            attempts.append({'method': 'SelectCtrl(GetCtrlInstID)', 'skipped': True, 'reason': 'hwp.SelectCtrl unavailable'})

        select_ctrl_object = getattr(hwp, 'select_ctrl', None)
        if callable(select_ctrl_object):
            try:
                raw = select_ctrl_object(ctrl)
                attempt = _record_attempt('select_ctrl(ctrl)', raw)
                if attempt.get('selection_succeeded'):
                    return {'selection_succeeded': True, 'method_used': attempt.get('method'), 'proof_strength': attempt.get('proof_strength'), 'target_ctrl_inst_id': target_inst_id or None, 'target_ctrl_id': target_ctrl_id or None, 'selected_control': attempt.get('selected_control'), 'attempts': attempts}
            except Exception as exc:
                _record_attempt('select_ctrl(ctrl)', error=exc)
        else:
            attempts.append({'method': 'select_ctrl(ctrl)', 'skipped': True, 'reason': 'hwp.select_ctrl unavailable'})

        anchor_pos = target_item.get('anchor_pos')
        if isinstance(anchor_pos, list) and len(anchor_pos) >= 3:
            try:
                _set_pos(hwp, int(anchor_pos[0]), int(anchor_pos[1]), int(anchor_pos[2]))
                find_ctrl = getattr(hwp, 'FindCtrl', None) or getattr(hwp, 'find_ctrl', None)
                if callable(find_ctrl):
                    raw = find_ctrl()
                    attempt = _record_attempt('anchor_pos+FindCtrl', raw, anchor_pos=anchor_pos)
                    if attempt.get('selection_succeeded'):
                        return {'selection_succeeded': True, 'method_used': attempt.get('method'), 'proof_strength': attempt.get('proof_strength'), 'target_ctrl_inst_id': target_inst_id or None, 'target_ctrl_id': target_ctrl_id or None, 'selected_control': attempt.get('selected_control'), 'attempts': attempts}
                else:
                    attempts.append({'method': 'anchor_pos+FindCtrl', 'skipped': True, 'reason': 'FindCtrl unavailable', 'anchor_pos': anchor_pos})
            except Exception as exc:
                _record_attempt('anchor_pos+FindCtrl', error=exc, anchor_pos=anchor_pos)
        else:
            attempts.append({'method': 'anchor_pos+FindCtrl', 'skipped': True, 'reason': 'target anchor_pos unavailable'})

        return {
            'selection_succeeded': False,
            'method_used': None,
            'proof_strength': 'unavailable',
            'target_ctrl_inst_id': target_inst_id or None,
            'target_ctrl_id': target_ctrl_id or None,
            'selected_control': self._bundle_selected_control_summary(hwp),
            'attempts': attempts,
        }

    def _bundle_resolve_control_target(self, hwp: Any, step: dict[str, Any], *, op_name: str, require_table: bool = False) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if expected_page <= 0:
            raise LocalCliRuntimeError(f'{op_name} requires positive expected_page')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'{op_name} section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'{op_name} around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        if len(matching) != 1:
            raise LocalCliRuntimeError(f'{op_name} target_id match count must be exactly 1, got {len(matching)} for {target_id!r}')
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if require_table and before_item.get('type') != 'tbl':
            raise LocalCliRuntimeError(f"{op_name} target must be a table control, got {before_item.get('type')!r}")
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(f"{op_name} proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}")
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'{op_name} cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(f'{op_name} expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}')
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(f'{op_name} page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}')
        return {
            'target_id': target_id,
            'expected_hash': expected_hash,
            'expected_page': expected_page,
            'page_from': page_from,
            'page_to': page_to,
            'max_controls': max_controls,
            'section_anchor': section_anchor,
            'around': around,
            'anchor_evidence': anchor_evidence,
            'around_evidence': around_evidence,
            'controls': controls,
            'enumeration_mode': enumeration_mode,
            'target_ctrl': target_ctrl,
            'before_item': before_item,
            'target_anchor_pos': target_anchor_pos,
        }

    def _bundle_control_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        max_controls = int(step.get('max_controls') or 2048)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        expected_page = step.get('expected_page')
        target_id = str(step.get('target_id') or '').strip() or None
        expected_hash = str(step.get('expected_hash') or '').strip() or None
        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        anchor_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('section_anchor') or '').strip() or None)
        around_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('around') or '').strip() or None)
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        page_filter_unknown_count = 0
        try:
            for index, ctrl in enumerate(controls):
                anchor_pos = _get_ctrl_anchor_pos(hwp, ctrl, option=1)
                snapshot: dict[str, Any] = {}
                page_evidence: dict[str, Any] = {'page': None, 'method': None}
                if anchor_pos is not None:
                    try:
                        _set_pos(hwp, anchor_pos[0], anchor_pos[1], anchor_pos[2])
                        snapshot = _snapshot_cursor_context(hwp)
                        page_evidence = self._bundle_page_evidence(hwp)
                    except Exception as exc:
                        snapshot = {'error': f'{type(exc).__name__}: {exc}'}
                page = page_evidence.get('page')
                if page_from is not None:
                    if page is None:
                        page_filter_unknown_count += 1
                    elif not (int(page_from) <= int(page) <= int(page_to)):
                        continue

                ctrl_id = self._bundle_control_scalar(ctrl, 'CtrlID')
                ctrl_inst_id = self._bundle_control_scalar(ctrl, 'CtrlInstID')
                user_desc = self._bundle_control_scalar(ctrl, 'UserDesc')
                type_name = self._bundle_control_scalar(ctrl, 'Type') or self._bundle_control_scalar(ctrl, 'ShapeType')
                bounds = {
                    key: self._bundle_control_scalar(ctrl, key)
                    for key in ('X', 'Y', 'Width', 'Height', 'HorzRelTo', 'VertRelTo')
                    if self._bundle_control_scalar(ctrl, key) not in (None, '')
                }
                proof_basis = {
                    'index': index,
                    'ctrl_id': ctrl_id,
                    'ctrl_inst_id': ctrl_inst_id,
                    'user_desc': user_desc,
                    'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
                    'bounds': bounds or None,
                    'page': page,
                    'type': type_name,
                }
                proof_hash = 'sha256:' + hashlib.sha256(json.dumps(proof_basis, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:24]
                stable_id = f"ctrl/{index}/{ctrl_id or 'unknown'}/{ctrl_inst_id or 'no-inst'}"
                text_preview = _preview_text(str(user_desc or ''), limit=120) if user_desc else None
                item = {
                    'target_id': stable_id,
                    'index': index,
                    'page': page,
                    'page_evidence': page_evidence,
                    'type': str(type_name or ctrl_id or 'control'),
                    'ctrl_id': ctrl_id,
                    'ctrl_inst_id': ctrl_inst_id,
                    'bounds': bounds or None,
                    'anchor_pos': list(anchor_pos) if anchor_pos is not None else None,
                    'text_preview': text_preview,
                    'proof_hash': proof_hash,
                    'nearby': {
                        'cell_addr': snapshot.get('cell_addr'),
                        'field_name': snapshot.get('field_name'),
                        'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
                    },
                    'source_evidence': {
                        'preexisting_control': True,
                        'editable_target': False,
                        'reason': 'inventory is read-only; use target id/hash as proof before any future edit primitive',
                    },
                }
                item['target_match'] = bool(
                    (target_id and (stable_id == target_id or str(ctrl_id or '') == target_id or str(ctrl_inst_id or '') == target_id))
                    and (not expected_hash or expected_hash == proof_hash)
                    and (not expected_page or page is None or int(expected_page) == int(page))
                )
                items.append(item)
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        if page_filter_unknown_count:
            warnings.append(
                f'page filter could not be applied to {page_filter_unknown_count} control(s) because page evidence was unavailable; those controls were retained.'
            )
        if target_id:
            matched = [item for item in items if item.get('target_match')]
            if len(matched) != 1:
                warnings.append(f'target proof is ambiguous or missing: matched {len(matched)} item(s) for target_id={target_id!r}.')
        return {
            'schema_version': 'local-cli/control-inventory/v1',
            'read_only': True,
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': step.get('section_anchor'),
                'page_from': page_from,
                'page_to': page_to,
                'around': step.get('around'),
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'control_count_total': len(controls),
            'control_count_returned': len(items),
            'items': items,
            'warnings': warnings,
        }

    def _bundle_table_frame_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        """Read-only grouping of table/frame-like controls and co-anchored graphics.

        This is intentionally an inventory primitive, not a repair primitive. It
        exposes the native control shape properties and anchor groups needed to
        decide whether a later row/cell/frame-flow mutation can be targeted
        safely.
        """

        max_controls = int(step.get('max_controls') or 2048)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        expected_page = step.get('expected_page')
        target_id = str(step.get('target_id') or '').strip() or None
        expected_hash = str(step.get('expected_hash') or '').strip() or None
        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        anchor_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('section_anchor') or '').strip() or None)
        around_evidence = self._bundle_find_anchor_evidence(hwp, str(step.get('around') or '').strip() or None)
        warnings: list[str] = []
        items: list[dict[str, Any]] = []
        page_filter_unknown_count = 0

        try:
            for index, ctrl in enumerate(controls):
                item, snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                page = item.get('page')
                if page_from is not None:
                    if page is None:
                        page_filter_unknown_count += 1
                    elif not (int(page_from) <= int(page) <= int(page_to)):
                        continue

                shape_props = self._shape_prop_snapshot(ctrl)
                anchor_key = '/'.join(str(part) for part in anchor_pos) if anchor_pos is not None else None
                enriched = dict(item)
                enriched['anchor_key'] = anchor_key
                enriched['shape_properties'] = shape_props
                enriched['nearby'] = {
                    **dict(enriched.get('nearby') or {}),
                    'cell_addr': snapshot.get('cell_addr'),
                    'is_cell': snapshot.get('is_cell'),
                    'selection_mode': snapshot.get('selection_mode'),
                    'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
                }
                enriched['target_match'] = bool(
                    (target_id and (item.get('target_id') == target_id or str(item.get('ctrl_id') or '') == target_id or str(item.get('ctrl_inst_id') or '') == target_id))
                    and (not expected_hash or expected_hash == item.get('proof_hash'))
                    and (not expected_page or page is None or int(expected_page) == int(page))
                )
                items.append(enriched)
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        groups_by_anchor: dict[str, dict[str, Any]] = {}
        for item in items:
            anchor_key = str(item.get('anchor_key') or 'unknown-anchor')
            group = groups_by_anchor.setdefault(
                anchor_key,
                {
                    'anchor_key': anchor_key,
                    'anchor_pos': item.get('anchor_pos'),
                    'pages': [],
                    'control_ids': [],
                    'types': [],
                    'items': [],
                    'has_table': False,
                    'has_graphic': False,
                },
            )
            page = item.get('page')
            if page is not None and page not in group['pages']:
                group['pages'].append(page)
            ctrl_id = item.get('ctrl_id') or item.get('type')
            if ctrl_id and ctrl_id not in group['types']:
                group['types'].append(ctrl_id)
            target = item.get('target_id')
            if target:
                group['control_ids'].append(target)
            group['items'].append({
                'target_id': item.get('target_id'),
                'page': item.get('page'),
                'type': item.get('type'),
                'ctrl_id': item.get('ctrl_id'),
                'proof_hash': item.get('proof_hash'),
                'shape_properties': item.get('shape_properties'),
                'text_preview': item.get('text_preview'),
            })
            ctrl_text = str(item.get('ctrl_id') or item.get('type') or '').lower()
            if ctrl_text == 'tbl' or 'table' in ctrl_text:
                group['has_table'] = True
            if ctrl_text in {'gso', 'pic'} or 'shape' in ctrl_text or 'graphic' in ctrl_text:
                group['has_graphic'] = True

        anchor_groups = list(groups_by_anchor.values())
        anchor_groups.sort(key=lambda group: (min(group.get('pages') or [999999]), str(group.get('anchor_key') or '')))
        for group in anchor_groups:
            group['pages'] = sorted(group.get('pages') or [])
            group['fit_risk'] = bool(group.get('has_table') and group.get('has_graphic'))
            if group['fit_risk']:
                group['fit_note'] = 'table and graphic share one anchor; direct graphic/table Width/Height may not repair row/cell/frame flow without a dedicated fit primitive'

        if page_filter_unknown_count:
            warnings.append(
                f'page filter could not be applied to {page_filter_unknown_count} control(s) because page evidence was unavailable; those controls were retained.'
            )
        if target_id:
            matched = [item for item in items if item.get('target_match')]
            if len(matched) != 1:
                warnings.append(f'target proof is ambiguous or missing: matched {len(matched)} item(s) for target_id={target_id!r}.')

        return {
            'schema_version': 'local-cli/table-frame-inventory/v1',
            'read_only': True,
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': step.get('section_anchor'),
                'page_from': page_from,
                'page_to': page_to,
                'around': step.get('around'),
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'control_count_total': len(controls),
            'control_count_returned': len(items),
            'items': items,
            'anchor_groups': anchor_groups,
            'warnings': warnings,
            'next_tool_gap': 'A mutating row/cell/frame-flow fit primitive is still required before changing container layout; this command is read-only inventory only.',
        }

    def _shape_prop_item(self, prop: Any, key: str) -> Any:
        item = getattr(prop, 'Item', None)
        if callable(item):
            try:
                return item(key)
            except Exception:
                pass
        try:
            return getattr(prop, key)
        except Exception:
            return None

    def _shape_prop_set_item(self, prop: Any, key: str, value: Any) -> None:
        setter = getattr(prop, 'SetItem', None)
        if callable(setter):
            setter(key, value)
            return
        setattr(prop, key, value)

    def _shape_prop_snapshot(self, ctrl: Any) -> dict[str, Any]:
        try:
            prop = getattr(ctrl, 'Properties')
        except Exception as exc:
            return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
        keys = (
            'Width',
            'Height',
            'HorzRelTo',
            'VertRelTo',
            'HorzAlign',
            'VertAlign',
            'HorzOffset',
            'VertOffset',
            'TreatAsChar',
            'TextWrap',
            'FlowWithText',
        )
        values = {key: self._shape_prop_item(prop, key) for key in keys}
        return {
            'available': True,
            'values': {key: value for key, value in values.items() if value is not None},
        }

    def _mm_to_hwp_unit(self, hwp: Any, value_mm: float) -> int:
        method = getattr(hwp, 'MiliToHwpUnit', None) or getattr(hwp, 'mili_to_hwp_unit', None)
        if callable(method):
            return int(round(float(method(float(value_mm)))))
        # Hancom HWPUNIT is 7200 units per inch; 1 mm is about 283.465 units.
        return int(round(float(value_mm) * 7200.0 / 25.4))

    def _hwp_unit_to_mm(self, hwp: Any, value_hu: float) -> float:
        method = getattr(hwp, 'HwpUnitToMili', None) or getattr(hwp, 'hwp_unit_to_mili', None)
        if callable(method):
            return float(method(float(value_hu)))
        return float(value_hu) * 25.4 / 7200.0

    def _bundle_enter_table_cell_for_ctrl(self, hwp: Any, ctrl: Any) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        select_cell = getattr(hwp, 'ShapeObjTableSelCell', None)
        text_box_edit = getattr(hwp, 'ShapeObjTextBoxEdit', None)
        haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        ctrl_inst_id = self._bundle_control_scalar(ctrl, 'CtrlInstID')
        ctrl_id = self._bundle_control_scalar(ctrl, 'CtrlID')
        target_item = {'ctrl_inst_id': ctrl_inst_id, 'ctrl_id': ctrl_id, 'anchor_pos': list(_get_ctrl_anchor_pos(hwp, ctrl, option=0) or [])}

        def _snapshot(label: str) -> dict[str, Any]:
            snap = self._bundle_compact_snapshot(hwp)
            attempts.append({'method': label, 'snapshot': snap})
            return snap

        def _try_text_edit(label: str) -> dict[str, Any]:
            if callable(text_box_edit):
                try:
                    raw = text_box_edit()
                    attempts.append({'method': f'{label}/ShapeObjTextBoxEdit()', 'result': bool(raw) if raw is not None else None})
                except Exception as exc:
                    attempts.append({'method': f'{label}/ShapeObjTextBoxEdit()', 'error': f'{type(exc).__name__}: {exc}'})
            elif callable(haction_run):
                try:
                    raw = haction_run('ShapeObjTextBoxEdit')
                    attempts.append({'method': f'{label}/HAction.Run(ShapeObjTextBoxEdit)', 'result': bool(raw) if raw is not None else None})
                except Exception as exc:
                    attempts.append({'method': f'{label}/HAction.Run(ShapeObjTextBoxEdit)', 'error': f'{type(exc).__name__}: {exc}'})
            else:
                attempts.append({'method': f'{label}/ShapeObjTextBoxEdit', 'skipped': True, 'reason': 'unavailable'})
            return _snapshot(f'{label}/after-text-edit')

        selection = self._bundle_select_control_exact(hwp, ctrl, target_item)
        attempts.append({'method': 'select-table-control', 'selection': selection})
        snap = _try_text_edit('exact-control-select') if selection.get('selection_succeeded') else _snapshot('exact-control-select/skipped-text-edit')
        if snap.get('is_cell') and not snap.get('has_selection') and int(snap.get('selection_mode') or 0) == 0:
            return {'is_cell': True, 'normal_edit_state': True, 'cell_addr': snap.get('cell_addr'), 'attempts': attempts}

        anchor_pos = _get_ctrl_anchor_pos(hwp, ctrl, option=0)
        if anchor_pos is not None:
            try:
                _set_pos(hwp, anchor_pos[0], anchor_pos[1], anchor_pos[2])
                find_ctrl = getattr(hwp, 'FindCtrl', None) or getattr(hwp, 'find_ctrl', None)
                if callable(find_ctrl):
                    attempts.append({'method': 'anchor_pos+FindCtrl', 'result': bool(find_ctrl())})
                snap = _try_text_edit('anchor_pos+FindCtrl')
                if snap.get('is_cell') and not snap.get('has_selection') and int(snap.get('selection_mode') or 0) == 0:
                    return {'is_cell': True, 'normal_edit_state': True, 'cell_addr': snap.get('cell_addr'), 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'anchor_pos+FindCtrl/ShapeObjTextBoxEdit', 'error': f'{type(exc).__name__}: {exc}'})

        if callable(select_cell):
            try:
                raw = select_cell()
                attempts.append({'method': 'ShapeObjTableSelCell diagnostic only (cell-block)', 'result': bool(raw) if raw is not None else None})
                snap = _snapshot('after-ShapeObjTableSelCell-diagnostic')
                if snap.get('is_cell'):
                    snap = _try_text_edit('cell-block-to-edit')
                    if snap.get('is_cell') and not snap.get('has_selection') and int(snap.get('selection_mode') or 0) == 0:
                        return {'is_cell': True, 'normal_edit_state': True, 'cell_addr': snap.get('cell_addr'), 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'ShapeObjTableSelCell diagnostic only (cell-block)', 'error': f'{type(exc).__name__}: {exc}'})

        final = self._bundle_compact_snapshot(hwp)
        return {
            'is_cell': bool(final.get('is_cell')),
            'normal_edit_state': bool(final.get('is_cell')) and not final.get('has_selection') and int(final.get('selection_mode') or 0) == 0,
            'cell_addr': final.get('cell_addr'),
            'attempts': attempts,
        }

    def _bundle_native_cell_margin_readback(
        self,
        hwp: Any,
        *,
        expected_cell_addr: Any = None,
    ) -> dict[str, Any]:
        """Read fresh native four-side cell margins for the current cell.

        ``pyhwpx.get_cell_margin`` can expose a cached wrapper value.  The
        native action refresh is therefore part of this observation contract;
        a failed refresh makes the observation unavailable instead of allowing
        a stale value to serve as persistence evidence.
        """
        source = 'HParameterSet.HShapeObject.ShapeTableCell.Margin*'
        observed_addr: list[int] | None = None
        try:
            get_cell_addr = getattr(hwp, 'get_cell_addr', None)
            if not callable(get_cell_addr):
                raise LocalCliRuntimeError('native cell identity getter is unavailable')
            observed_addr = _normalize_cell_addr_value(get_cell_addr(as_='tuple'))
            if observed_addr is None:
                raise LocalCliRuntimeError('native cell identity is missing or invalid')
            expected_addr = _normalize_cell_addr_value(expected_cell_addr) if expected_cell_addr is not None else None
            if expected_cell_addr is not None and (expected_addr is None or observed_addr != expected_addr):
                raise LocalCliRuntimeError(
                    'native cell identity does not match the requested target; '
                    f'observed={observed_addr!r}; expected={expected_cell_addr!r}'
                )

            parameter_root = getattr(hwp, 'HParameterSet')
            shape = getattr(parameter_root, 'HShapeObject')
            hset = getattr(shape, 'HSet')
            get_default = getattr(getattr(hwp, 'HAction'), 'GetDefault', None)
            if not callable(get_default):
                raise LocalCliRuntimeError('TablePropertyDialog GetDefault is unavailable')
            refresh_result = get_default('TablePropertyDialog', hset)
            if refresh_result is not True:
                raise LocalCliRuntimeError('TablePropertyDialog GetDefault returned no positive success')
            cell = getattr(shape, 'ShapeTableCell')
            margins = {
                'left': getattr(cell, 'MarginLeft'),
                'right': getattr(cell, 'MarginRight'),
                'top': getattr(cell, 'MarginTop'),
                'bottom': getattr(cell, 'MarginBottom'),
            }
            normalized = _normalize_cell_margin_readback(margins)
            if normalized is None:
                raise LocalCliRuntimeError(f'native four-side cell-margin values are invalid: {margins!r}')
            return {
                'available': True,
                'refresh_succeeded': True,
                'cell_addr': observed_addr,
                'value': normalized,
                'source': source,
            }
        except Exception as exc:
            error = str(exc) if isinstance(exc, LocalCliRuntimeError) else f'{type(exc).__name__}: {exc}'
            return {
                'available': False,
                'refresh_succeeded': False,
                'cell_addr': observed_addr,
                'error': error,
                'source': source,
            }

    def _bundle_table_cell_metrics(self, hwp: Any, ctrl: Any) -> dict[str, Any]:
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, ctrl)
            metrics: dict[str, Any] = {'enter': enter}
            if not enter.get('is_cell'):
                return {**metrics, 'available': False, 'error': 'could not place caret inside target table cell'}
            for key, method_name, kwargs in (
                ('cell_addr', 'get_cell_addr', {'as_': 'tuple'}),
                ('row_height_hu', 'get_row_height', {'as_': 'hwpunit'}),
                ('row_height_mm', 'get_row_height', {'as_': 'mm'}),
                ('table_height_hu', 'get_table_height', {'as_': 'hwpunit'}),
                ('table_height_mm', 'get_table_height', {'as_': 'mm'}),
                ('table_inside_margin_hu', 'get_table_inside_margin', {'as_': 'hwpunit'}),
                ('table_outside_margin_hu', 'get_table_outside_margin', {'as_': 'hwpunit'}),
            ):
                method = getattr(hwp, method_name, None)
                if not callable(method):
                    metrics[key] = {'unavailable': method_name}
                    continue
                try:
                    metrics[key] = method(**kwargs)
                except Exception as exc:
                    metrics[key] = {'error': f'{type(exc).__name__}: {exc}'}
            target_cell_addr = _normalize_cell_addr_value(enter.get('cell_addr'))
            if target_cell_addr is None:
                target_cell_addr = _normalize_cell_addr_value(metrics.get('cell_addr'))
            metrics['target_cell_addr'] = target_cell_addr
            margin_readback = self._bundle_native_cell_margin_readback(
                hwp,
                expected_cell_addr=target_cell_addr,
            )
            metrics['cell_margin_readback'] = margin_readback
            metrics['cell_margin_hu'] = (
                margin_readback.get('value')
                if margin_readback.get('available') is True
                else {'error': margin_readback.get('error', 'native margin readback unavailable')}
            )
            if margin_readback.get('cell_addr') is not None:
                metrics['cell_addr'] = margin_readback.get('cell_addr')
            char_raw = self._style_parameter_snapshot(
                hwp,
                'CharShape',
                'HCharShape',
                ('Height',),
            )
            para_raw = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('LineSpacing', 'LineSpacingType'),
            )
            char_values = char_raw.get('values') if isinstance(char_raw.get('values'), dict) else {}
            para_values = para_raw.get('values') if isinstance(para_raw.get('values'), dict) else {}
            metrics['char_height_raw'] = char_values.get('Height')
            metrics['para_line_spacing'] = para_values.get('LineSpacing')
            metrics['para_line_spacing_type'] = para_values.get('LineSpacingType')
            metrics['vertical_align'] = self._bundle_table_cell_vertical_align(hwp)
            metrics['style_snapshot'] = {'char_shape': char_raw, 'para_shape': para_raw}
            metrics['cell_addr_valid'] = _normalize_cell_addr_value(metrics.get('cell_addr')) is not None
            metrics['row_height_hu_valid'] = _valid_numeric_readback(metrics.get('row_height_hu'))
            metrics['cell_margin_hu_valid'] = _normalize_cell_margin_readback(metrics.get('cell_margin_hu')) is not None
            metrics['cell_margin_hu_available'] = bool(
                metrics['cell_margin_hu_valid'] and margin_readback.get('refresh_succeeded') is True
            )
            metrics['cell_identity_matches_target'] = bool(
                target_cell_addr is not None
                and _normalize_cell_addr_value(metrics.get('cell_addr')) == target_cell_addr
            )
            metrics['vertical_align_available'] = _normalize_vertical_align_readback(metrics.get('vertical_align')) is not None
            metrics['available'] = (
                bool(metrics['cell_addr_valid'])
                and bool(metrics['row_height_hu_valid'])
                and bool(metrics['cell_margin_hu_available'])
                and bool(metrics['cell_identity_matches_target'])
            )
            return metrics
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    @staticmethod
    def _bundle_table_cell_vertical_align(hwp: Any) -> dict[str, Any]:
        """Read the native vertical alignment of the current table cell.

        Hancom exposes this value after ``TablePropertyDialog`` populates
        ``HShapeObject.ShapeTableCell``. The COM value is an integer enum:
        0=top, 1=center, 2=bottom.
        """
        try:
            parameter_root = getattr(hwp, 'HParameterSet')
            shape = getattr(parameter_root, 'HShapeObject')
            get_default = getattr(getattr(hwp, 'HAction'), 'GetDefault', None)
            if not callable(get_default):
                raise LocalCliRuntimeError('TablePropertyDialog GetDefault is unavailable')
            default_result = get_default('TablePropertyDialog', getattr(shape, 'HSet'))
            if default_result is not True:
                raise LocalCliRuntimeError(
                    'TablePropertyDialog GetDefault did not return positive success; '
                    f'result={default_result!r}'
                )
            cell = getattr(shape, 'ShapeTableCell')
            raw_value = getattr(cell, 'VertAlign')
            if isinstance(raw_value, bool):
                raise LocalCliRuntimeError('TablePropertyDialog VertAlign is boolean, not a native enum')
            if not isinstance(raw_value, int) or raw_value not in {0, 1, 2}:
                raise LocalCliRuntimeError(
                    'TablePropertyDialog VertAlign enum is invalid (not an integral native enum); '
                    f'value={raw_value!r}'
                )
            value = raw_value
            names = {0: 'top', 1: 'center', 2: 'bottom'}
            if value not in names:
                raise LocalCliRuntimeError(f'TablePropertyDialog VertAlign enum is invalid: {value!r}')
            return {
                'available': True,
                'refresh_succeeded': True,
                'value': value,
                'name': names.get(value),
                'source': 'HParameterSet.HShapeObject.ShapeTableCell.VertAlign',
            }
        except Exception as exc:
            return {
                'available': False,
                'refresh_succeeded': False,
                'error': f'{type(exc).__name__}: {exc}',
                'source': 'HParameterSet.HShapeObject.ShapeTableCell.VertAlign',
            }

    def _bundle_exact_control_select_proof(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='exact_control_select_proof')
        before_snapshot = self._bundle_compact_snapshot(hwp)
        selection = self._bundle_select_control_exact(hwp, resolved['target_ctrl'], resolved['before_item'])
        after_snapshot = self._bundle_compact_snapshot(hwp)
        if not selection.get('selection_succeeded'):
            raise LocalCliRuntimeError(
                'exact_control_select_proof could not select/prove the target control; '
                f'attempts={selection.get("attempts")!r}'
            )
        warnings = []
        if selection.get('proof_strength') != 'exact-ctrl-inst-id':
            warnings.append('Exact CtrlInstID selection was unavailable or unverified; fallback selection proof was used.')
        return {
            'schema_version': 'local-cli/exact-control-select-proof/v1',
            'read_only': True,
            'mutation': None,
            'succeeded': True,
            'enumeration_mode': resolved['enumeration_mode'],
            'scope': {
                'section_anchor': resolved['section_anchor'],
                'page_from': resolved['page_from'],
                'page_to': resolved['page_to'],
                'around': resolved['around'],
            },
            'anchors': {'section_anchor': resolved['anchor_evidence'], 'around': resolved['around_evidence']},
            'target_proof': {
                'target_id': resolved['target_id'],
                'expected_hash': resolved['expected_hash'],
                'expected_page': resolved['expected_page'],
                'matched_before': resolved['before_item'],
                'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
            },
            'selection_proof': selection,
            'method_used': selection.get('method_used'),
            'proof_strength': selection.get('proof_strength'),
            'before': before_snapshot,
            'after': after_snapshot,
            'warnings': warnings,
        }

    def _bundle_metric_probe(self, hwp: Any, method_name: str, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
        method = getattr(hwp, method_name, None)
        if not callable(method):
            return {'available': False, 'method': method_name, 'error': 'unavailable'}
        try:
            value = method(**(kwargs or {}))
        except Exception as exc:
            return {'available': False, 'method': method_name, 'error': f'{type(exc).__name__}: {exc}'}
        return {
            'available': True,
            'method': method_name,
            'value': self._macro_result_preview(value),
            'raw_type': type(value).__name__,
        }

    def _bundle_call_cell_navigation(self, hwp: Any, action_name: str) -> dict[str, Any]:
        py_method = getattr(hwp, action_name, None)
        haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        attempts: list[dict[str, Any]] = []
        if callable(py_method):
            try:
                raw = py_method()
                return {
                    'method': f'{action_name}()',
                    'result': bool(raw) if raw is not None else None,
                    'raw_type': type(raw).__name__,
                    'attempts': [{'method': f'{action_name}()', 'result': bool(raw) if raw is not None else None}],
                }
            except Exception as exc:
                attempts.append({'method': f'{action_name}()', 'error': f'{type(exc).__name__}: {exc}'})
        if callable(haction_run):
            try:
                raw = haction_run(action_name)
                attempts.append({'method': f'HAction.Run({action_name})', 'result': bool(raw) if raw is not None else None})
                return {
                    'method': f'HAction.Run({action_name})',
                    'result': bool(raw) if raw is not None else None,
                    'raw_type': type(raw).__name__,
                    'attempts': attempts,
                }
            except Exception as exc:
                attempts.append({'method': f'HAction.Run({action_name})', 'error': f'{type(exc).__name__}: {exc}'})
        if not attempts:
            attempts.append({'method': action_name, 'error': 'unavailable'})
        return {'method': None, 'result': None, 'attempts': attempts}

    def _bundle_call_move_pos(self, hwp: Any, move_id: int) -> dict[str, Any]:
        method = getattr(hwp, 'move_pos', None) or getattr(hwp, 'MovePos', None)
        if not callable(method):
            return {'method': 'move_pos', 'move_id': move_id, 'error': 'unavailable'}
        try:
            raw = method(move_id)
        except Exception as exc:
            return {'method': 'move_pos', 'move_id': move_id, 'error': f'{type(exc).__name__}: {exc}'}
        return {'method': 'move_pos', 'move_id': move_id, 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__}

    def _bundle_navigation_probe_once(
        self,
        hwp: Any,
        ctrl: Any,
        *,
        label: str,
        kind: str,
        action_name: str | None = None,
        move_id: int | None = None,
    ) -> dict[str, Any]:
        enter = self._bundle_enter_table_cell_for_ctrl(hwp, ctrl)
        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_cell = self._bundle_current_cell_addr_tuple(hwp)
        if not enter.get('is_cell') or not enter.get('normal_edit_state'):
            return {
                'label': label,
                'kind': kind,
                'enter': enter,
                'before': before_snapshot,
                'before_cell': list(before_cell) if before_cell is not None else None,
                'skipped': True,
                'reason': 'could not enter target table in normal edit state',
            }
        if kind == 'action' and action_name:
            call = self._bundle_call_cell_navigation(hwp, action_name)
        elif kind == 'move_pos' and move_id is not None:
            call = self._bundle_call_move_pos(hwp, move_id)
        else:
            call = {'error': 'invalid navigation probe request'}
        after_snapshot = self._bundle_compact_snapshot(hwp)
        after_cell = self._bundle_current_cell_addr_tuple(hwp)
        moved = before_cell is not None and after_cell is not None and before_cell != after_cell
        return {
            'label': label,
            'kind': kind,
            'enter': enter,
            'before': before_snapshot,
            'before_cell': list(before_cell) if before_cell is not None else None,
            'call': call,
            'after': after_snapshot,
            'after_cell': list(after_cell) if after_cell is not None else None,
            'moved': moved,
        }

    def _bundle_sha256_text(self, value: str) -> str:
        return 'sha256:' + hashlib.sha256(str(value).encode('utf-8', errors='replace')).hexdigest()

    def _bundle_sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return 'sha256:' + digest.hexdigest()

    def _bundle_bindata_manifest(self, path: Path) -> dict[str, Any]:
        if not path.exists() or not path.is_file():
            raise LocalCliRuntimeError(f'BinData manifest source is missing: {path}')
        items: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(path, 'r') as archive:
                for name in sorted(info.filename for info in archive.infolist() if info.filename.startswith('BinData/')):
                    data = archive.read(name)
                    items.append(
                        {
                            'name': name,
                            'size': len(data),
                            'sha256': hashlib.sha256(data).hexdigest(),
                        }
                    )
        except Exception as exc:
            raise LocalCliRuntimeError(f'Could not read HWPX BinData manifest from {path}: {type(exc).__name__}: {exc}') from exc
        canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {
            'available': True,
            'path': str(path),
            'count': len(items),
            'items': items,
            'manifest_hash': self._bundle_sha256_text(canonical),
        }

    def _bundle_table_inventory_signature(
        self,
        *,
        target_id: str,
        rows: int,
        cols: int,
        document_text_hash: str,
        text_char_count: int,
        nonempty_line_count: int,
        div0_count: int,
    ) -> dict[str, Any]:
        payload = {
            'target_id': target_id,
            'rows': rows,
            'cols': cols,
            'addresses_seen_zero_based_col_row': [[col, row] for row in range(rows) for col in range(cols)],
            'document_text_hash': document_text_hash,
            'text_char_count': text_char_count,
            'nonempty_line_count': nonempty_line_count,
            'div0_count': div0_count,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {**payload, 'inventory_hash': self._bundle_sha256_text(canonical)}

    def _bundle_table_width_snapshot(self, hwp: Any, ctrl: Any) -> dict[str, Any]:
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, ctrl)
            if not enter.get('is_cell'):
                raise LocalCliRuntimeError('table_column_width_exact could not enter the target native table')

            def _read(method_name: str, **kwargs: Any) -> Any:
                method = getattr(hwp, method_name, None)
                if not callable(method):
                    raise LocalCliRuntimeError(f'table_column_width_exact requires pyhwpx {method_name}')
                try:
                    return method(**kwargs)
                except Exception as exc:
                    raise LocalCliRuntimeError(f'table_column_width_exact {method_name} failed: {type(exc).__name__}: {exc}') from exc

            entry_addr_raw = _read('get_cell_addr', as_='tuple')
            entry_addr = list(entry_addr_raw) if isinstance(entry_addr_raw, (tuple, list)) else [entry_addr_raw]
            rows = int(_read('get_row_num'))
            cols = int(_read('get_col_num'))
            if rows <= 0 or cols <= 0:
                raise LocalCliRuntimeError(f'table_column_width_exact read invalid native table dimensions: rows={rows}, cols={cols}')

            left_navigation: list[dict[str, Any]] = []
            for _ in range(min(cols + 1, 101)):
                before = list(_read('get_cell_addr', as_='tuple'))
                if int(before[0]) <= 0:
                    break
                nav = self._bundle_call_cell_navigation(hwp, 'TableLeftCell')
                after = list(_read('get_cell_addr', as_='tuple'))
                left_navigation.append({'before': before, 'after': after, 'navigation': nav})
                if after == before:
                    raise LocalCliRuntimeError('table_column_width_exact could not navigate to the first native table column')
            origin_addr = list(_read('get_cell_addr', as_='tuple'))
            if len(origin_addr) < 2 or int(origin_addr[0]) != 0:
                raise LocalCliRuntimeError(f'table_column_width_exact requires a first-column origin, got {origin_addr!r}')

            widths_mm: list[float] = []
            addresses: list[list[int]] = []
            right_navigation: list[dict[str, Any]] = []
            for index in range(cols):
                addr = list(_read('get_cell_addr', as_='tuple'))
                width = float(_read('get_col_width', as_='mm'))
                if not math.isfinite(width) or width <= 0.0:
                    raise LocalCliRuntimeError(f'table_column_width_exact read invalid column width at {addr!r}: {width!r}')
                widths_mm.append(width)
                addresses.append([int(addr[0]), int(addr[1])])
                if index < cols - 1:
                    nav = self._bundle_call_cell_navigation(hwp, 'TableRightCell')
                    after = list(_read('get_cell_addr', as_='tuple'))
                    right_navigation.append({'before': addr, 'after': after, 'navigation': nav})
                    if after == addr:
                        raise LocalCliRuntimeError(f'table_column_width_exact could not navigate from column {index} to the next column')

            return {
                'available': True,
                'entry_cell_addr': entry_addr,
                'origin_cell_addr': origin_addr,
                'rows': rows,
                'cols': cols,
                'widths_mm': widths_mm,
                'width_sum_mm': sum(widths_mm),
                'table_width_mm': float(_read('get_table_width', as_='mm')),
                'table_height_mm': float(_read('get_table_height', as_='mm')),
                'row_height_mm': float(_read('get_row_height', as_='mm')),
                'addresses_seen_zero_based_col_row': addresses,
                'left_navigation': left_navigation,
                'right_navigation': right_navigation,
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_set_table_column_widths(self, hwp: Any, widths_mm: list[float]) -> dict[str, Any]:
        method = getattr(hwp, 'set_col_width', None)
        if not callable(method):
            raise LocalCliRuntimeError('table_column_width_exact requires the admitted pyhwpx set_col_width primitive')
        if not widths_mm or any(not math.isfinite(float(value)) or float(value) <= 0.0 for value in widths_mm):
            raise LocalCliRuntimeError('table_column_width_exact received invalid finite positive widths')
        try:
            raw = method([float(value) for value in widths_mm], as_='mm')
        except Exception as exc:
            raise LocalCliRuntimeError(
                f'table_column_width_exact set_col_width(widths_mm, as_=mm) failed: {type(exc).__name__}: {exc}'
            ) from exc
        if raw is False:
            raise LocalCliRuntimeError('table_column_width_exact set_col_width returned False')
        return {
            'method': 'pyhwpx.set_col_width(widths_mm, as_=mm)',
            'result': bool(raw) if raw is not None else None,
            'requested_widths_mm': [float(value) for value in widths_mm],
        }

    def _bundle_table_column_width_exact(self, handle: LocalCliRuntimeHandle, step: dict[str, Any]) -> dict[str, Any]:
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('table_column_width_exact requires confirm_layout=true')
        resolved = self._bundle_resolve_control_target(handle.hwp, step, op_name='table_column_width_exact', require_table=True)
        target_ctrl = resolved['target_ctrl']
        expected_preimage = str(step.get('expected_preimage_sha256') or '').strip().lower()
        expected_cell_inventory_hash = str(step.get('expected_cell_inventory_hash') or '').strip().lower()
        expected_text_hash = str(step.get('expected_document_text_hash') or '').strip().lower()
        expected_bindata_hash = str(step.get('expected_bindata_manifest_hash') or '').strip().lower()
        for field_name, value in (
            ('expected_preimage_sha256', expected_preimage),
            ('expected_cell_inventory_hash', expected_cell_inventory_hash),
            ('expected_document_text_hash', expected_text_hash),
            ('expected_bindata_manifest_hash', expected_bindata_hash),
        ):
            if value.startswith('sha256:'):
                value = value[7:]
            if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
                raise LocalCliRuntimeError(f'table_column_width_exact requires a full SHA-256 value for {field_name}')

        expected_rows = int(step.get('expected_rows') or 0)
        expected_cols = int(step.get('expected_cols') or 0)
        expected_total_width = float(step.get('expected_total_width_mm') or 0.0)
        expected_height = float(step.get('expected_table_height_mm') or 0.0)
        expected_control_count = int(step.get('expected_control_count') or 0)
        expected_char_count = int(step.get('expected_text_char_count') or 0)
        expected_line_count = int(step.get('expected_nonempty_line_count') or 0)
        expected_div0_count = int(step.get('expected_div0_count') or 0)
        requested_widths = [float(value) for value in (step.get('requested_widths_mm') or [])]
        if expected_rows <= 0 or expected_cols <= 0 or expected_control_count <= 0:
            raise LocalCliRuntimeError('table_column_width_exact received invalid expected dimensions/control count')
        if not math.isfinite(expected_total_width) or not math.isfinite(expected_height) or expected_total_width <= 0.0 or expected_height <= 0.0:
            raise LocalCliRuntimeError('table_column_width_exact received invalid expected table dimensions')
        if len(requested_widths) != expected_cols or any(not math.isfinite(value) or value <= 0.0 for value in requested_widths):
            raise LocalCliRuntimeError('table_column_width_exact requested_widths_mm does not match expected_cols or contains invalid values')
        if abs(sum(requested_widths) - expected_total_width) > 0.05:
            raise LocalCliRuntimeError('table_column_width_exact fixed-total guard failed before mutation')

        working_copy_path = Path(str(handle.working_copy_path))
        actual_preimage = self._bundle_sha256_file(working_copy_path)
        if actual_preimage != expected_preimage:
            raise LocalCliRuntimeError(
                f'table_column_width_exact preimage SHA-256 mismatch: expected {expected_preimage!r}, got {actual_preimage!r}'
            )

        before_text = self._get_live_document_text(handle, purpose='table-column-width-exact:before')
        before_text_hash = self._bundle_sha256_text(before_text)
        before_text_guard = {
            'hash': before_text_hash,
            'char_count': len(before_text),
            'nonempty_line_count': len([line.strip() for line in before_text.splitlines() if line.strip()]),
            'div0_count': before_text.count('#DIV/0!'),
        }
        if (
            before_text_hash != expected_text_hash
            or before_text_guard['char_count'] != expected_char_count
            or before_text_guard['nonempty_line_count'] != expected_line_count
            or before_text_guard['div0_count'] != expected_div0_count
        ):
            raise LocalCliRuntimeError(f'table_column_width_exact document text guard failed: expected={step!r}; actual={before_text_guard!r}')

        before_control_map = self._capture_control_map_signature(handle.hwp)
        if int(before_control_map.get('control_count') or 0) != expected_control_count:
            raise LocalCliRuntimeError(
                f'table_column_width_exact native control-count guard failed: expected {expected_control_count}, got {before_control_map.get("control_count")}'
            )

        before_snapshot_path = self._snapshot_temp_hwpx(handle, purpose='table-column-width-exact-before')
        before_bindata = self._bundle_bindata_manifest(before_snapshot_path)
        if before_bindata.get('manifest_hash') != expected_bindata_hash:
            raise LocalCliRuntimeError(
                f'table_column_width_exact BinData guard failed: expected {expected_bindata_hash!r}, got {before_bindata.get("manifest_hash")!r}'
            )

        before_table = self._bundle_table_width_snapshot(handle.hwp, target_ctrl)
        if before_table['rows'] != expected_rows or before_table['cols'] != expected_cols:
            raise LocalCliRuntimeError(
                f'table_column_width_exact native dimension guard failed: expected {expected_rows}x{expected_cols}, got {before_table["rows"]}x{before_table["cols"]}'
            )
        if abs(float(before_table['table_width_mm']) - expected_total_width) > 0.2:
            raise LocalCliRuntimeError(
                f'table_column_width_exact fixed table width guard failed: expected {expected_total_width} mm, got {before_table["table_width_mm"]} mm'
            )
        if abs(float(before_table['table_height_mm']) - expected_height) > 0.5:
            raise LocalCliRuntimeError(
                f'table_column_width_exact table height guard failed: expected {expected_height} mm, got {before_table["table_height_mm"]} mm'
            )
        before_inventory = self._bundle_table_inventory_signature(
            target_id=resolved['target_id'],
            rows=before_table['rows'],
            cols=before_table['cols'],
            document_text_hash=before_text_hash,
            text_char_count=before_text_guard['char_count'],
            nonempty_line_count=before_text_guard['nonempty_line_count'],
            div0_count=before_text_guard['div0_count'],
        )
        if before_inventory['inventory_hash'] != expected_cell_inventory_hash:
            raise LocalCliRuntimeError(
                f'table_column_width_exact cell/text inventory guard failed: expected {expected_cell_inventory_hash!r}, got {before_inventory["inventory_hash"]!r}'
            )

        mutation_started = False
        rollback: dict[str, Any] = {'attempted': False, 'succeeded': False, 'error': None}
        original_pos = None
        try:
            original_pos = _get_pos(handle.hwp)
        except Exception:
            original_pos = None
        try:
            entered = self._bundle_enter_table_cell_for_ctrl(handle.hwp, target_ctrl)
            if not entered.get('is_cell'):
                raise LocalCliRuntimeError('table_column_width_exact could not re-enter the target table for mutation')
            operation = self._bundle_set_table_column_widths(handle.hwp, requested_widths)
            mutation_started = True
            after_table = self._bundle_table_width_snapshot(handle.hwp, target_ctrl)
            after_text = self._get_live_document_text(handle, purpose='table-column-width-exact:after')
            after_text_guard = {
                'hash': self._bundle_sha256_text(after_text),
                'char_count': len(after_text),
                'nonempty_line_count': len([line.strip() for line in after_text.splitlines() if line.strip()]),
                'div0_count': after_text.count('#DIV/0!'),
            }
            after_control_map = self._capture_control_map_signature(handle.hwp)
            after_snapshot_path = self._snapshot_temp_hwpx(handle, purpose='table-column-width-exact-after')
            after_bindata = self._bundle_bindata_manifest(after_snapshot_path)
            after_inventory = self._bundle_table_inventory_signature(
                target_id=resolved['target_id'],
                rows=after_table['rows'],
                cols=after_table['cols'],
                document_text_hash=after_text_guard['hash'],
                text_char_count=after_text_guard['char_count'],
                nonempty_line_count=after_text_guard['nonempty_line_count'],
                div0_count=after_text_guard['div0_count'],
            )
            width_readback_ok = all(abs(float(actual) - float(requested)) <= 0.25 for actual, requested in zip(after_table['widths_mm'], requested_widths))
            if not width_readback_ok:
                raise LocalCliRuntimeError(
                    f'table_column_width_exact width readback mismatch: requested={requested_widths!r}, actual={after_table["widths_mm"]!r}'
                )
            if after_table['rows'] != before_table['rows'] or after_table['cols'] != before_table['cols']:
                raise LocalCliRuntimeError('table_column_width_exact changed native table row/column counts')
            if abs(float(after_table['table_width_mm']) - expected_total_width) > 0.2:
                raise LocalCliRuntimeError('table_column_width_exact changed the fixed native table total width')
            if after_text_guard != before_text_guard:
                raise LocalCliRuntimeError(f'table_column_width_exact changed document text/#DIV/0! guard: before={before_text_guard!r}; after={after_text_guard!r}')
            control_proof = self._assert_control_map_unchanged(
                before=before_control_map,
                after=after_control_map,
                operation='table_column_width_exact',
            )
            if after_bindata.get('manifest_hash') != before_bindata.get('manifest_hash'):
                raise LocalCliRuntimeError('table_column_width_exact changed the in-memory HWPX BinData manifest')
            if after_inventory['inventory_hash'] != before_inventory['inventory_hash']:
                raise LocalCliRuntimeError('table_column_width_exact changed the guarded cell/text inventory signature')
            return {
                'schema_version': 'local-cli/table-column-width-exact/v1',
                'read_only': False,
                'mutation': 'native-table-column-width',
                'succeeded': True,
                'doc_backed_api_actions': ['pyhwpx.set_col_width(widths_mm, as_=mm)'],
                'scope': {
                    'section_anchor': resolved['section_anchor'],
                    'page_from': resolved['page_from'],
                    'page_to': resolved['page_to'],
                    'around': resolved['around'],
                },
                'target_proof': {
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'matched_before': resolved['before_item'],
                    'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
                },
                'preimage': {
                    'working_copy_path': str(working_copy_path),
                    'expected_sha256': expected_preimage,
                    'actual_sha256': actual_preimage,
                    'matched': True,
                },
                'inventory_guard': {
                    'expected_hash': expected_cell_inventory_hash,
                    'before': before_inventory,
                    'after': after_inventory,
                    'matched': True,
                },
                'text_guard': {'before': before_text_guard, 'after': after_text_guard, 'matched': True},
                'control_map_guard': control_proof,
                'bindata_guard': {
                    'expected_hash': expected_bindata_hash,
                    'before': before_bindata,
                    'after': after_bindata,
                    'matched': True,
                },
                'table_readback': {
                    'expected_rows': expected_rows,
                    'expected_cols': expected_cols,
                    'expected_total_width_mm': expected_total_width,
                    'before': before_table,
                    'after': after_table,
                    'requested_widths_mm': requested_widths,
                    'width_readback_matched': width_readback_ok,
                },
                'operation': operation,
                'rollback': rollback,
                'snapshot_paths': {'before': str(before_snapshot_path), 'after': str(after_snapshot_path)},
                'warnings': ['Working copy remains unsaved; require close-without-save rollback evidence and fresh full-document Hancom render before promotion.'],
            }
        except Exception as exc:
            if mutation_started:
                rollback['attempted'] = True
                try:
                    entered = self._bundle_enter_table_cell_for_ctrl(handle.hwp, target_ctrl)
                    if not entered.get('is_cell'):
                        raise LocalCliRuntimeError('rollback could not re-enter target table')
                    self._bundle_set_table_column_widths(handle.hwp, [float(value) for value in before_table['widths_mm']])
                    rollback_table = self._bundle_table_width_snapshot(handle.hwp, target_ctrl)
                    rollback['succeeded'] = all(
                        abs(float(actual) - float(expected)) <= 0.25
                        for actual, expected in zip(rollback_table['widths_mm'], before_table['widths_mm'])
                    )
                    rollback['after'] = rollback_table
                    if not rollback['succeeded']:
                        raise LocalCliRuntimeError(f'rollback width readback mismatch: {rollback_table["widths_mm"]!r}')
                except Exception as rollback_exc:
                    rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
            if rollback.get('attempted') and not rollback.get('succeeded'):
                raise LocalCliRuntimeError(
                    f'table_column_width_exact failed and rollback failed: primary={type(exc).__name__}: {exc}; rollback={rollback!r}'
                ) from exc
            raise
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(handle.hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass


    def _bundle_table_cell_structure_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='table_cell_structure_exact', require_table=True)
        max_controls = int(step.get('max_controls') or 2048)
        page_from = resolved.get('page_from')
        page_to = resolved.get('page_to') or page_from
        before_snapshot = self._bundle_compact_snapshot(hwp)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
            entered_snapshot = self._bundle_compact_snapshot(hwp)
            metrics: dict[str, Any] = {}
            if enter.get('is_cell'):
                for key, method_name, kwargs in (
                    ('cell_addr_str', 'get_cell_addr', {'as_': 'str'}),
                    ('cell_addr_tuple', 'get_cell_addr', {'as_': 'tuple'}),
                    ('row_count', 'get_row_num', {}),
                    ('col_num', 'get_col_num', {}),
                    ('row_height_hu', 'get_row_height', {'as_': 'hwpunit'}),
                    ('row_height_mm', 'get_row_height', {'as_': 'mm'}),
                    ('col_width_hu', 'get_col_width', {'as_': 'hwpunit'}),
                    ('col_width_mm', 'get_col_width', {'as_': 'mm'}),
                    ('table_height_hu', 'get_table_height', {'as_': 'hwpunit'}),
                    ('table_height_mm', 'get_table_height', {'as_': 'mm'}),
                    ('table_width_hu', 'get_table_width', {'as_': 'hwpunit'}),
                    ('table_width_mm', 'get_table_width', {'as_': 'mm'}),
                    ('cell_margin_hu', 'get_cell_margin', {'as_': 'hwpunit'}),
                    ('cell_margin_mm', 'get_cell_margin', {'as_': 'mm'}),
                    ('table_inside_margin_hu', 'get_table_inside_margin', {'as_': 'hwpunit'}),
                    ('table_outside_margin_hu', 'get_table_outside_margin', {'as_': 'hwpunit'}),
                ):
                    metrics[key] = self._bundle_metric_probe(hwp, method_name, kwargs)
            navigation: list[dict[str, Any]] = []
            for label, action_name in (
                ('left', 'TableLeftCell'),
                ('right', 'TableRightCell'),
                ('up', 'TableUpperCell'),
                ('down', 'TableLowerCell'),
            ):
                navigation.append(self._bundle_navigation_probe_once(hwp, resolved['target_ctrl'], label=label, kind='action', action_name=action_name))
            for label, move_id in (
                ('move_pos_left_cell', 100),
                ('move_pos_right_cell', 101),
                ('move_pos_up_cell', 102),
                ('move_pos_down_cell', 103),
                ('move_pos_row_start', 104),
                ('move_pos_row_end', 105),
                ('move_pos_col_top', 106),
                ('move_pos_col_bottom', 107),
            ):
                navigation.append(self._bundle_navigation_probe_once(hwp, resolved['target_ctrl'], label=label, kind='move_pos', move_id=move_id))
            inferred_addresses = sorted(
                {
                    tuple(item.get(key) or [])
                    for item in navigation
                    for key in ('before_cell', 'after_cell')
                    if isinstance(item.get(key), list) and len(item.get(key)) >= 2
                }
            )
            inferred_max_row_index = max((int(cell[1]) for cell in inferred_addresses), default=None)
            inferred_max_col_index = max((int(cell[0]) for cell in inferred_addresses), default=None)
            table_frame_inventory = self._bundle_table_frame_inventory(
                hwp,
                {
                    'op': 'table_frame_inventory',
                    'page_from': page_from,
                    'page_to': page_to,
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'max_controls': max_controls,
                },
            )
            same_anchor_group = None
            target_anchor = '/'.join(str(part) for part in resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None
            for group in table_frame_inventory.get('anchor_groups') or []:
                if group.get('anchor_key') == target_anchor:
                    same_anchor_group = group
                    break
            any_navigation_moved = any(bool(item.get('moved')) for item in navigation)
            row_count_value = metrics.get('row_count', {}).get('value') if isinstance(metrics.get('row_count'), dict) else None
            clipping_owner_hypothesis = (
                'single-cell-or-non-navigable table container/frame is the active limiter; the co-anchored graphic/table flow, not the graphic size alone, likely clips content'
                if not any_navigation_moved
                else 'table appears cell-navigable; row/cell-level split or fit primitive may be targetable after rendered proof'
            )
            if row_count_value in (1, '1'):
                clipping_owner_hypothesis = 'documented row count reports one row; treat target as a single-cell container/frame unless a richer table property proves otherwise'
            return {
                'schema_version': 'local-cli/table-cell-structure-exact/v1',
                'read_only': True,
                'mutation': None,
                'succeeded': True,
                'doc_backed_api_actions': [
                    'Ctrl.GetCtrlInstID + hwp.SelectCtrl for exact control selection',
                    'ShapeObjTextBoxEdit to enter table A1 in normal edit state',
                    'get_cell_addr/get_row_num/get_col_num/get_row_height/get_col_width/get_table_height/get_table_width/get_cell_margin for read-only metrics',
                    'TableLeftCell/TableRightCell/TableUpperCell/TableLowerCell and move_pos(100..107) for read-only navigation probes',
                ],
                'enumeration_mode': resolved['enumeration_mode'],
                'scope': {
                    'section_anchor': resolved['section_anchor'],
                    'page_from': resolved['page_from'],
                    'page_to': resolved['page_to'],
                    'around': resolved['around'],
                },
                'anchors': {'section_anchor': resolved['anchor_evidence'], 'around': resolved['around_evidence']},
                'target_proof': {
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'matched_before': resolved['before_item'],
                    'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
                    'shape_properties': self._shape_prop_snapshot(resolved['target_ctrl']),
                },
                'before': before_snapshot,
                'enter': enter,
                'entered_snapshot': entered_snapshot,
                'metrics': metrics,
                'navigation': navigation,
                'navigation_summary': {
                    'any_navigation_moved': any_navigation_moved,
                    'addresses_seen_zero_based_col_row': [list(cell) for cell in inferred_addresses],
                    'inferred_min_rows_from_navigation': None if inferred_max_row_index is None else inferred_max_row_index + 1,
                    'inferred_min_cols_from_navigation': None if inferred_max_col_index is None else inferred_max_col_index + 1,
                },
                'same_anchor_group': same_anchor_group,
                'table_frame_inventory': table_frame_inventory,
                'clipping_owner_hypothesis': clipping_owner_hypothesis,
                'next_safe_step_hint': 'Do not force TableSplitTable if navigation cannot leave A1; prefer a documented container/cell/frame-flow primitive or accept the blocker.',
                'warnings': [],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_set_cell_margin_values(self, hwp: Any, margins: Mapping[str, Any]) -> dict[str, Any]:
        method = getattr(hwp, 'set_cell_margin', None)
        if not callable(method):
            raise LocalCliRuntimeError('cell_format_exact requires pyhwpx set_cell_margin')
        normalized = _normalize_cell_margin_readback(margins)
        if normalized is None:
            raise LocalCliRuntimeError(f'cell_format_exact cell-margin values are invalid: {margins!r}')
        args = tuple(normalized[key] for key in _CELL_MARGIN_KEYS)
        kwargs = {'as_': 'hwpunit'}
        signature = None
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            # Some COM proxies do not expose a Python signature.  The pinned
            # pyhwpx order is still invoked once; never probe alternatives.
            pass
        if signature is not None:
            try:
                signature.bind(*args, **kwargs)
            except TypeError as exc:
                raise LocalCliRuntimeError(
                    'cell_format_exact set_cell_margin does not support the documented '
                    'four-side HWPUNIT call'
                ) from exc
        label = 'set_cell_margin(left,right,top,bottom, as_=hwpunit)'
        try:
            raw = method(*args, **kwargs)
        except Exception as exc:
            raise LocalCliMutationError(
                f'cell_format_exact set_cell_margin failed after invocation: {type(exc).__name__}: {exc}',
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'succeeded': False},
            ) from exc
        result = bool(raw) if raw is not None else None
        attempt = {'method': label, 'result': result}
        if raw is not None and not bool(raw):
            raise LocalCliMutationError(
                'cell_format_exact set_cell_margin returned false after invocation',
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'succeeded': False},
            )
        return {'method': label, 'result': result, 'attempts': [attempt], 'arguments': dict(normalized)}

    def _bundle_set_uniform_cell_margin(self, hwp: Any, value_hu: int) -> dict[str, Any]:
        return self._bundle_set_cell_margin_values(
            hwp,
            {key: value_hu for key in _CELL_MARGIN_KEYS},
        )

    def _bundle_apply_cell_fill_color(self, hwp: Any, fill_color: str) -> dict[str, Any]:
        value = str(fill_color or '').strip().upper()
        if re.fullmatch(r'#[0-9A-F]{6}', value) is None:
            raise LocalCliRuntimeError('cell_format_exact fill_color must be #RRGGBB')
        rgb = tuple(int(value[offset:offset + 2], 16) for offset in (1, 3, 5))

        def action_ok(raw: Any, *, action_name: str) -> bool:
            # COM automation methods commonly return None on success, but an
            # explicit False is a native failure and must never be reported as
            # a successful cell mutation.
            if raw is not None and not bool(raw):
                raise LocalCliRuntimeError(f'cell_format_exact {action_name} returned false')
            return True

        def coerce_rgb(raw: Any) -> tuple[int, int, int] | None:
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                try:
                    channels = tuple(int(raw[index]) for index in range(3))
                except (TypeError, ValueError):
                    return None
                return channels if all(0 <= channel <= 255 for channel in channels) else None
            if isinstance(raw, Mapping):
                values = []
                for names in (('r', 'red'), ('g', 'green'), ('b', 'blue')):
                    selected = next((raw[name] for name in names if name in raw), None)
                    if selected is None:
                        return None
                    values.append(selected)
                return coerce_rgb(values)
            if isinstance(raw, bool):
                return None
            if isinstance(raw, int):
                # Win32 COLORREF stores RGB as 0x00BBGGRR.
                return (raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF)
            if isinstance(raw, str):
                text = raw.strip().upper()
                if re.fullmatch(r'#[0-9A-F]{6}', text):
                    return (
                        int(text[1:3], 16),
                        int(text[3:5], 16),
                        int(text[5:7], 16),
                    )
                if text.startswith('0X'):
                    try:
                        return coerce_rgb(int(text, 16))
                    except ValueError:
                        return None
            for names in (('red', 'r'), ('green', 'g'), ('blue', 'b')):
                if not any(hasattr(raw, name) for name in names):
                    break
            else:
                return coerce_rgb({
                    'r': next(getattr(raw, name) for name in ('red', 'r') if hasattr(raw, name)),
                    'g': next(getattr(raw, name) for name in ('green', 'g') if hasattr(raw, name)),
                    'b': next(getattr(raw, name) for name in ('blue', 'b') if hasattr(raw, name)),
                })
            return None

        def readback(*, refresh_default: bool = True) -> dict[str, Any]:
            parameter_root = getattr(hwp, 'HParameterSet', None)
            parameter_set = getattr(parameter_root, 'HCellBorderFill', None) if parameter_root is not None else None
            action = getattr(hwp, 'HAction', None)
            get_default = getattr(action, 'GetDefault', None)
            if parameter_set is not None:
                hset = getattr(parameter_set, 'HSet', parameter_set)
                if refresh_default and callable(get_default):
                    raw_default = get_default('CellFill', hset)
                    action_ok(raw_default, action_name='CellFill GetDefault')
                fill_attr = getattr(parameter_set, 'FillAttr', None)
                if fill_attr is None:
                    fill_attr = getattr(hset, 'FillAttr', None)
                if fill_attr is not None:
                    for attribute in ('WinBrushFaceColor', 'Color', 'FaceColor'):
                        try:
                            observed = coerce_rgb(getattr(fill_attr, attribute))
                        except Exception:
                            observed = None
                        if observed is not None:
                            if observed != rgb:
                                raise LocalCliRuntimeError(
                                    f'cell_format_exact fill-color native readback mismatch: expected={rgb!r}; observed={observed!r}'
                                )
                            return {
                                'source': (
                                    'HAction.GetDefault(CellFill)'
                                    if refresh_default and callable(get_default)
                                    else 'HCellBorderFill.FillAttr'
                                ),
                                'observed_rgb': list(observed),
                                'expected_rgb': list(rgb),
                            }
            for getter_name in ('get_cell_fill_color', 'get_cell_fill', 'cell_fill_color'):
                getter = getattr(hwp, getter_name, None)
                if not callable(getter):
                    continue
                try:
                    observed = coerce_rgb(getter())
                except Exception:
                    observed = None
                if observed is None:
                    continue
                if observed != rgb:
                    raise LocalCliRuntimeError(
                        f'cell_format_exact fill-color native readback mismatch: expected={rgb!r}; observed={observed!r}'
                    )
                return {
                    'source': getter_name,
                    'observed_rgb': list(observed),
                    'expected_rgb': list(rgb),
                }
            raise LocalCliRuntimeError(
                'cell_format_exact fill-color native readback was unavailable; refusing to report success'
            )

        def read_current_fill() -> dict[str, Any] | None:
            parameter_root = getattr(hwp, 'HParameterSet', None)
            parameter_set = getattr(parameter_root, 'HCellBorderFill', None) if parameter_root is not None else None
            action = getattr(hwp, 'HAction', None)
            get_default = getattr(action, 'GetDefault', None)
            if parameter_set is not None:
                hset = getattr(parameter_set, 'HSet', parameter_set)
                if callable(get_default):
                    try:
                        raw_default = get_default('CellFill', hset)
                        if raw_default is not None and not bool(raw_default):
                            return None
                    except Exception:
                        return None
                fill_attr = getattr(parameter_set, 'FillAttr', None) or getattr(hset, 'FillAttr', None)
                if fill_attr is not None:
                    for attribute in ('WinBrushFaceColor', 'Color', 'FaceColor'):
                        try:
                            observed = coerce_rgb(getattr(fill_attr, attribute))
                        except Exception:
                            observed = None
                        if observed is not None:
                            return {'source': 'HAction.GetDefault(CellFill)', 'rgb': list(observed)}
            for getter_name in ('get_cell_fill_color', 'get_cell_fill', 'cell_fill_color'):
                getter = getattr(hwp, getter_name, None)
                if callable(getter):
                    try:
                        observed = coerce_rgb(getter())
                    except Exception:
                        observed = None
                    if observed is not None:
                        return {'source': getter_name, 'rgb': list(observed)}
            return None

        method = getattr(hwp, 'cell_fill', None)
        if callable(method):
            preimage = read_current_fill()
            mutation_attempted = False
            try:
                mutation_attempted = True
                raw = method(rgb)
                action_ok(raw, action_name='cell_fill')
                readback_proof = readback()
            except Exception as exc:
                rollback: dict[str, Any] = {
                    'attempted': False,
                    'succeeded': False,
                    'preimage': preimage,
                }
                if preimage is not None:
                    rollback['attempted'] = True
                    try:
                        rollback_raw = method(tuple(preimage['rgb']))
                        action_ok(rollback_raw, action_name='cell_fill rollback')
                        observed_after_rollback = read_current_fill()
                        rollback['observed'] = observed_after_rollback
                        rollback['succeeded'] = bool(
                            observed_after_rollback
                            and observed_after_rollback.get('rgb') == preimage.get('rgb')
                        )
                    except Exception as rollback_exc:
                        rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
                raise LocalCliMutationError(
                    f'cell_format_exact fill-color mutation/readback failed: {type(exc).__name__}: {exc}',
                    mutation_may_have_persisted=mutation_attempted and not bool(rollback.get('succeeded')),
                    rollback=rollback,
                ) from exc
            return {
                'method': 'cell_fill((r,g,b))',
                'rgb': list(rgb),
                'result': bool(raw) if raw is not None else None,
                'preimage': preimage,
                'rollback': {'attempted': False, 'succeeded': False},
                'readback_proof': readback_proof,
            }
        parameter_root = getattr(hwp, 'HParameterSet', None)
        parameter_set = getattr(parameter_root, 'HCellBorderFill', None) if parameter_root is not None else None
        action = getattr(hwp, 'HAction', None)
        get_default = getattr(action, 'GetDefault', None)
        execute = getattr(action, 'Execute', None)
        if parameter_set is None or not callable(get_default) or not callable(execute):
            raise LocalCliRuntimeError(
                'cell_format_exact requires pyhwpx cell_fill or the documented HCellBorderFill CellFill parameter set'
            )

        hset = getattr(parameter_set, 'HSet', parameter_set)
        fill_attr = getattr(parameter_set, 'FillAttr', None)
        if fill_attr is None:
            fill_attr = getattr(hset, 'FillAttr', None)
        if fill_attr is None:
            raise LocalCliRuntimeError('cell_format_exact CellFill parameter set has no FillAttr member')
        rgb_color = getattr(hwp, 'RGBColor', None)
        color_value = rgb_color(*rgb) if callable(rgb_color) else rgb
        initial_default_rgb: tuple[int, int, int] | None = None
        fallback_mutation_attempted = False
        try:
            action_ok(get_default('CellFill', hset), action_name='CellFill GetDefault')
            for attribute in ('WinBrushFaceColor', 'Color', 'FaceColor'):
                try:
                    initial_default_rgb = coerce_rgb(getattr(fill_attr, attribute))
                except Exception:
                    initial_default_rgb = None
                if initial_default_rgb is not None:
                    break
            fill_attr.Type = 1
            fill_attr.WinBrushFaceColor = color_value
            fallback_mutation_attempted = True
            raw = execute('CellFill', hset)
            action_ok(raw, action_name='CellFill Execute')
            readback_proof = readback(refresh_default=initial_default_rgb is not None)
        except Exception as exc:
            rollback: dict[str, Any] = {
                'attempted': False,
                'succeeded': False,
                'preimage': {'rgb': list(initial_default_rgb)} if initial_default_rgb is not None else None,
            }
            if fallback_mutation_attempted and initial_default_rgb is not None:
                rollback['attempted'] = True
                try:
                    fill_attr.Type = 1
                    fill_attr.WinBrushFaceColor = (
                        rgb_color(*initial_default_rgb) if callable(rgb_color) else initial_default_rgb
                    )
                    rollback_raw = execute('CellFill', hset)
                    action_ok(rollback_raw, action_name='CellFill rollback')
                    observed_after_rollback = read_current_fill()
                    rollback['observed'] = observed_after_rollback
                    rollback['succeeded'] = bool(
                        observed_after_rollback
                        and observed_after_rollback.get('rgb') == list(initial_default_rgb)
                    )
                except Exception as rollback_exc:
                    rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
            if isinstance(exc, LocalCliRuntimeError) and not fallback_mutation_attempted:
                raise
            raise LocalCliMutationError(
                f'cell_format_exact HAction.Execute(CellFill) failed: {type(exc).__name__}: {exc}',
                mutation_may_have_persisted=fallback_mutation_attempted and not bool(rollback.get('succeeded')),
                rollback=rollback,
            ) from exc
        return {
            'method': 'HAction.Execute(CellFill)',
            'rgb': list(rgb),
            'color_value': color_value,
            'result': bool(raw) if raw is not None else None,
            # A few older pyhwpx-compatible wrappers expose the parameter set
            # but do not populate it from GetDefault.  In that case the
            # post-execute FillAttr value is the only available proof and a
            # second GetDefault call would add no information.  Real native
            # bindings that provide an initial value are refreshed after the
            # mutation so a stale requested value cannot masquerade as proof.
            'readback_proof': readback_proof,
            'preimage': {'rgb': list(initial_default_rgb)} if initial_default_rgb is not None else None,
            'rollback': {'attempted': False, 'succeeded': False},
        }

    def _bundle_apply_cell_border_none(self, hwp: Any) -> dict[str, Any]:
        action = getattr(hwp, 'HAction', None)
        hset_root = getattr(hwp, 'HParameterSet', None)
        parameter_set = getattr(hset_root, 'HCellBorderFill', None) if hset_root is not None else None
        get_default = getattr(action, 'GetDefault', None)
        execute = getattr(action, 'Execute', None)
        hset_value = getattr(parameter_set, 'HSet', parameter_set) if parameter_set is not None else None
        set_item = getattr(hset_value, 'SetItem', None) if hset_value is not None else None
        if parameter_set is None or not callable(get_default) or not callable(execute) or not callable(set_item):
            raise LocalCliRuntimeError(
                'cell_format_exact border-none requires the explicit HCellBorderFill parameter set and diagonal readback; '
                'TableCellBorderNo fallback cannot prove diagonal NONE'
            )

        line_type_factory = getattr(hwp, 'HwpLineType', None)
        line_width_factory = getattr(hwp, 'HwpLineWidth', None)
        line_type = line_type_factory('None') if callable(line_type_factory) else 'None'
        line_width = line_width_factory('0.1mm') if callable(line_width_factory) else '0.1mm'
        flags = (
            'SlashFlag',
            'BackSlashFlag',
            'CounterSlashFlag',
            'CounterBackSlashFlag',
            'CenterLineFlag',
            'CrookedSlashFlag',
            'CrookedSlashFlag1',
            'CrookedSlashFlag2',
        )
        side_names = ('Left', 'Right', 'Top', 'Bottom')
        expected_type_items = tuple(f'BorderType{side}' for side in side_names) + ('DiagonalType',)
        expected_line_items = tuple(
            list(expected_type_items)
            + [f'BorderWidth{side}' for side in side_names]
            + ['DiagonalWidth']
        )

        def action_ok(raw: Any) -> bool:
            return raw is None or bool(raw)

        def read_parameter_item(name: str) -> Any:
            errors: list[str] = []
            for owner, owner_name in ((parameter_set, 'parameter set'), (hset_value, 'HSet')):
                try:
                    value = getattr(owner, name)
                except Exception as exc:
                    errors.append(f'{owner_name}: {type(exc).__name__}: {exc}')
                    continue
                if value is not None:
                    return value
                errors.append(f'{owner_name}: returned null')
            for accessor_name in ('GetItem', 'Item'):
                try:
                    accessor = getattr(hset_value, accessor_name)
                except Exception as exc:
                    errors.append(f'{accessor_name}: {type(exc).__name__}: {exc}')
                    continue
                if not callable(accessor):
                    errors.append(f'{accessor_name}: not callable')
                    continue
                try:
                    value = accessor(name)
                except Exception as exc:
                    errors.append(f'{accessor_name}: {type(exc).__name__}: {exc}')
                    continue
                if value is not None:
                    return value
                errors.append(f'{accessor_name}: returned null')
            detail = '; '.join(errors[-4:])
            raise LocalCliRuntimeError(f'cell_format_exact border-none readback unavailable for {name}: {detail}')

        def is_none_line(value: Any) -> bool:
            if isinstance(value, bool):
                return not value
            if isinstance(value, (int, float)):
                return value == 0
            normalized = str(value).strip().lower()
            return normalized in {'0', 'none', 'line:none'} or normalized.endswith(':none')

        def is_clear_flag(value: Any) -> bool:
            if isinstance(value, bool):
                return not value
            if isinstance(value, (int, float)):
                return value == 0
            return str(value).strip().lower() in {'', '0', 'false', 'none'}

        preimage_values: dict[str, Any] = {}
        preimage_missing: list[str] = []
        mutation_attempted = False
        try:
            if not action_ok(get_default('CellBorderFill', hset_value)):
                raise LocalCliRuntimeError('cell_format_exact border-none GetDefault returned false')
            for name in expected_line_items + flags + ('ApplyTo',):
                try:
                    preimage_values[name] = read_parameter_item(name)
                except LocalCliRuntimeError:
                    preimage_missing.append(name)
            for side in side_names:
                mutation_attempted = True
                set_item(f'BorderType{side}', line_type)
                set_item(f'BorderWidth{side}', line_width)
            # Hancom's documented BorderFill schema uses numeric PIT_UI*
            # values for DiagonalType/DiagonalWidth. BorderTypeDiagonal is
            # not a native item and can leave a persisted diagonal line
            # untouched. ApplyTo=0 is the selected-cell value; ApplyTo=1
            # leaves a cell's persisted diagonal unchanged on Hancom.
            set_item('DiagonalType', 0)
            set_item('DiagonalWidth', 0)
            for flag in flags:
                set_item(flag, 0)
            set_item('ApplyTo', 0)
            raw = execute('CellBorderFill', hset_value)
            if not action_ok(raw):
                raise LocalCliRuntimeError('cell_format_exact border-none Execute returned false')
            if not action_ok(get_default('CellBorderFill', hset_value)):
                raise LocalCliRuntimeError('cell_format_exact border-none readback GetDefault returned false')

            readback_values = {name: read_parameter_item(name) for name in expected_line_items}
            readback_flags = {name: read_parameter_item(name) for name in flags}
            non_none_sides = [name for name in expected_type_items if not is_none_line(readback_values[name])]
            non_clear_flags = [name for name, value in readback_flags.items() if not is_clear_flag(value)]
            if non_none_sides or non_clear_flags:
                details = ', '.join(non_none_sides + non_clear_flags)
                raise LocalCliRuntimeError(
                    f'cell_format_exact border-none diagonal/all-side readback did not prove NONE: {details}'
                )
            preimage = {
                'available': not preimage_missing,
                'values': preimage_values,
                'missing': preimage_missing,
            }
            return {
                'method': 'HAction.Execute(CellBorderFill border none)',
                'result': bool(raw) if raw is not None else None,
                'border_sides': ['Left', 'Right', 'Top', 'Bottom', 'Diagonal'],
                'diagonal_none_requested': True,
                'preimage': preimage,
                'rollback': {'attempted': False, 'succeeded': False},
                'readback_proof': {
                    'action': 'HAction.GetDefault(CellBorderFill)',
                    'all_sides_none': True,
                    'diagonal_none': True,
                    'diagonal_flags_clear': True,
                    'verified_items': list(expected_line_items) + list(flags),
                },
            }
        except LocalCliRuntimeError as exc:
            rollback: dict[str, Any] = {
                'attempted': False,
                'succeeded': False,
                'preimage': {
                    'available': not preimage_missing,
                    'values': preimage_values,
                    'missing': preimage_missing,
                },
            }
            if mutation_attempted and not preimage_missing:
                rollback['attempted'] = True
                try:
                    for name, value in preimage_values.items():
                        set_item(name, value)
                    rollback_raw = execute('CellBorderFill', hset_value)
                    if not action_ok(rollback_raw):
                        raise LocalCliRuntimeError('cell_format_exact border-none rollback Execute returned false')
                    if not action_ok(get_default('CellBorderFill', hset_value)):
                        raise LocalCliRuntimeError('cell_format_exact border-none rollback GetDefault returned false')
                    rollback_values = {name: read_parameter_item(name) for name in preimage_values}
                    rollback['observed'] = rollback_values
                    rollback['succeeded'] = rollback_values == preimage_values
                except Exception as rollback_exc:
                    rollback['error'] = f'{type(rollback_exc).__name__}: {rollback_exc}'
            if not mutation_attempted:
                raise
            raise LocalCliMutationError(
                f'cell_format_exact border-none mutation/readback failed: {type(exc).__name__}: {exc}',
                mutation_may_have_persisted=not bool(rollback.get('succeeded')),
                rollback=rollback,
            ) from exc
        except Exception as parameter_exc:
            if not mutation_attempted:
                raise LocalCliRuntimeError(
                    f'cell_format_exact HAction.Execute(CellBorderFill border none) failed: '
                    f'{type(parameter_exc).__name__}: {parameter_exc}'
                ) from parameter_exc
            raise LocalCliMutationError(
                f'cell_format_exact border-none mutation failed: {type(parameter_exc).__name__}: {parameter_exc}',
                mutation_may_have_persisted=True,
                rollback={
                    'attempted': False,
                    'succeeded': False,
                    'preimage': {
                        'available': not preimage_missing,
                        'values': preimage_values,
                        'missing': preimage_missing,
                    },
                },
            ) from parameter_exc

    def _bundle_cell_format_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('cell_format_exact requires confirm_layout=true')
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='cell_format_exact', require_table=True)
        target_ctrl = resolved['target_ctrl']
        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
        if not before_metrics.get('available'):
            raise LocalCliRuntimeError(f'cell_format_exact cannot read target cell metrics: {before_metrics.get("error")}')
        requested_margin_hu: int | None = None
        if step.get('cell_margin_hu') is not None or step.get('cell_margin_mm') is not None:
            requested_margin_hu = (
                int(round(float(step.get('cell_margin_hu'))))
                if step.get('cell_margin_hu') is not None
                else self._mm_to_hwp_unit(hwp, float(step.get('cell_margin_mm')))
            )
            before_margin = _normalize_cell_margin_readback(before_metrics.get('cell_margin_hu'))
            expected_margin = {key: requested_margin_hu for key in _CELL_MARGIN_KEYS}
            if before_margin == expected_margin:
                raise LocalCliRuntimeError(
                    f'cell_format_exact requested cell margins are already present: {expected_margin!r}'
                )
        elif step.get('fill_color') is None and step.get('border') is None:
            requested_vertical_align = str(step.get('vertical_align') or '').strip()
            before_vertical = _normalize_vertical_align_readback(before_metrics.get('vertical_align'))
            if before_vertical is None:
                raise LocalCliRuntimeError(
                    'cell_format_exact cannot mutate vertical alignment without a fresh native before readback'
                )
            if requested_vertical_align == before_vertical['name']:
                raise LocalCliRuntimeError(
                    f'cell_format_exact requested vertical alignment is already present: {requested_vertical_align!r}'
                )

        mutation_original_pos = None
        try:
            mutation_original_pos = _get_pos(hwp)
        except Exception:
            mutation_original_pos = None
        mutation_attempted = False
        operation_kind: str | None = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, target_ctrl)
            if not enter.get('is_cell'):
                raise LocalCliRuntimeError('cell_format_exact cannot enter the target table cell for mutation')
            raw_results: list[dict[str, Any]] = []
            run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if callable(run):
                try:
                    raw = run('TableCellBlock')
                    raw_results.append({'method': 'TableCellBlock', 'result': bool(raw) if raw is not None else None})
                    if raw is not None and not bool(raw):
                        raise LocalCliRuntimeError('cell_format_exact TableCellBlock returned false')
                except Exception as exc:
                    if isinstance(exc, LocalCliRuntimeError):
                        raise
                    raw_results.append({'method': 'TableCellBlock', 'error': f'{type(exc).__name__}: {exc}'})
            if step.get('cell_margin_hu') is not None or step.get('cell_margin_mm') is not None:
                assert requested_margin_hu is not None
                value_hu = requested_margin_hu
                operation_kind = 'set-cell-margin'
                mutation_attempted = True
                margin_result = self._bundle_set_uniform_cell_margin(hwp, value_hu)
                raw_results.append({'method': margin_result.get('method'), 'result': margin_result.get('result'), 'value_hu': value_hu, 'attempts': margin_result.get('attempts')})
                operation = {
                    'op': 'set-cell-margin',
                    'cell_margin_hu': value_hu,
                    'requested_margins_hu': {key: value_hu for key in _CELL_MARGIN_KEYS},
                    'cell_margin_mm': self._hwp_unit_to_mm(hwp, value_hu),
                    'raw_results': raw_results,
                    'enter': enter,
                }
            elif step.get('fill_color') is not None:
                operation_kind = 'fill-color'
                fill_result = self._bundle_apply_cell_fill_color(hwp, str(step.get('fill_color')))
                mutation_attempted = True
                raw_results.append(fill_result)
                operation = {
                    'op': 'fill-color',
                    'fill_color': str(step.get('fill_color')).upper(),
                    'raw_results': raw_results,
                    'enter': enter,
                }
            elif step.get('border') is not None:
                operation_kind = 'border-none'
                border_result = self._bundle_apply_cell_border_none(hwp)
                mutation_attempted = True
                raw_results.append(border_result)
                operation = {
                    'op': 'border-none',
                    'border': 'none',
                    'raw_results': raw_results,
                    'enter': enter,
                }
            else:
                vertical_align = str(step.get('vertical_align') or '').strip()
                action_map = {
                    'top': 'TableVAlignTop',
                    'center': 'TableVAlignCenter',
                    'bottom': 'TableVAlignBottom',
                }
                action_name = action_map.get(vertical_align)
                if not action_name:
                    raise LocalCliRuntimeError('cell_format_exact requires vertical_align top, center, or bottom')
                if not callable(run):
                    raise LocalCliRuntimeError(f'cell_format_exact requires HAction.Run for {action_name}')
                operation_kind = 'vertical-align'
                mutation_attempted = True
                raw = run(action_name)
                raw_results.append({'method': action_name, 'result': bool(raw) if raw is not None else None})
                if raw is not None and not bool(raw):
                    raise LocalCliMutationError(
                        f'cell_format_exact {action_name} returned false after invocation',
                        mutation_may_have_persisted=True,
                        rollback={'attempted': False, 'succeeded': False},
                    )
                operation = {
                    'op': 'vertical-align',
                    'vertical_align': vertical_align,
                    'action_name': action_name,
                    'raw_results': raw_results,
                    'enter': enter,
                }
        except LocalCliMutationError:
            raise
        except LocalCliRuntimeError as exc:
            if mutation_attempted:
                raise LocalCliMutationError(
                    f'cell_format_exact {operation_kind or "mutation"} failed after invocation: {type(exc).__name__}: {exc}',
                    mutation_may_have_persisted=True,
                    rollback={'attempted': False, 'succeeded': False},
                ) from exc
            raise
        except Exception as exc:
            if mutation_attempted:
                raise LocalCliMutationError(
                    f'cell_format_exact {operation_kind or "mutation"} failed after invocation: {type(exc).__name__}: {exc}',
                    mutation_may_have_persisted=True,
                    rollback={'attempted': False, 'succeeded': False},
                ) from exc
            raise LocalCliRuntimeError(f'cell_format_exact mutation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            if mutation_original_pos is not None and len(mutation_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(mutation_original_pos[0]), int(mutation_original_pos[1]), int(mutation_original_pos[2]))
                except Exception:
                    pass

        try:
            after_snapshot = self._bundle_compact_snapshot(hwp)
            after_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
            changed_metrics = {
                key: {'before': before_metrics.get(key), 'after': after_metrics.get(key)}
                for key in sorted(set(before_metrics) | set(after_metrics))
                if before_metrics.get(key) != after_metrics.get(key)
            }
            _require_observed_cell_format_mutation(
                str(operation.get('op') or ''),
                before_metrics,
                after_metrics,
                changed_metrics,
                expected_vertical_align=(
                    str(operation.get('vertical_align') or '')
                    if operation.get('op') == 'vertical-align'
                    else None
                ),
                expected_cell_margin_hu=(
                    operation.get('requested_margins_hu')
                    if operation.get('op') == 'set-cell-margin'
                    else None
                ),
                expected_cell_addr=before_metrics.get('cell_addr'),
            )

            post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=int(resolved['max_controls']))
            if len(post_controls) != len(resolved['controls']):
                raise LocalCliRuntimeError(f'cell_format_exact control count changed unexpectedly: before={len(resolved["controls"])}, after={len(post_controls)}')
            post_target_items: list[dict[str, Any]] = []
            post_original_pos = None
            try:
                post_original_pos = _get_pos(hwp)
            except Exception:
                post_original_pos = None
            try:
                for index, ctrl in enumerate(post_controls):
                    item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                    if item.get('target_id') == resolved['target_id']:
                        post_target_items.append(item)
            finally:
                if post_original_pos is not None and len(post_original_pos) >= 3:
                    try:
                        _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                    except Exception:
                        pass
            if len(post_target_items) != 1:
                raise LocalCliRuntimeError(f'cell_format_exact post-mutation target count must be exactly 1, got {len(post_target_items)} for {resolved["target_id"]!r}')
        except LocalCliMutationError:
            raise
        except Exception as exc:
            raise LocalCliMutationError(
                f'cell_format_exact {operation_kind or "mutation"} post-mutation readback/proof failed: '
                f'{type(exc).__name__}: {exc}',
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'succeeded': False},
            ) from exc

        warnings = ['This primitive mutates one target table cell format only; rendered before/after proof is required before accepting the working copy.']
        if operation.get('op') == 'vertical-align':
            warnings.append('Vertical alignment includes native ShapeTableCell.VertAlign before/after readback; rendered review remains required for final acceptance.')
        return {
            'schema_version': 'local-cli/cell-format-exact/v1',
            'read_only': False,
            'mutation': 'cell-format',
            'succeeded': True,
            'enumeration_mode': resolved['enumeration_mode'],
            'scope': {
                'section_anchor': resolved['section_anchor'],
                'page_from': resolved['page_from'],
                'page_to': resolved['page_to'],
                'around': resolved['around'],
            },
            'anchors': {'section_anchor': resolved['anchor_evidence'], 'around': resolved['around_evidence']},
            'target_proof': {
                'target_id': resolved['target_id'],
                'expected_hash': resolved['expected_hash'],
                'expected_page': resolved['expected_page'],
                'matched_before': resolved['before_item'],
                'matched_after': post_target_items[0],
                'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
            },
            'operation': operation,
            'before': before_snapshot,
            'after': after_snapshot,
            'metrics_before': before_metrics,
            'metrics_after': after_metrics,
            'changed_metrics': changed_metrics,
            'post_mutation': {'control_count_before': len(resolved['controls']), 'control_count_after': len(post_controls), 'post_target_count': len(post_target_items)},
            'warnings': warnings,
        }


    def _bundle_page_table_items(self, hwp: Any, *, page_from: int | None, page_to: int | None, max_controls: int) -> list[dict[str, Any]]:
        controls, _mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        items: list[dict[str, Any]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('type') != 'tbl':
                    continue
                page = item.get('page')
                if page is not None and page_from is not None:
                    if not (int(page_from) <= int(page) <= int(page_to or page_from)):
                        continue
                items.append({
                    'target_id': item.get('target_id'),
                    'page': page,
                    'proof_hash': item.get('proof_hash'),
                    'shape_properties': item.get('shape_properties'),
                    'text_preview': item.get('text_preview'),
                })
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        return items

    def _run_table_split_action(self, hwp: Any) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        action = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        if callable(action):
            try:
                raw = action('TableSplitTable')
                attempts.append({'method': 'HAction.Run(TableSplitTable)', 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__})
                return {'succeeded': raw is None or bool(raw), 'method': 'HAction.Run', 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'HAction.Run(TableSplitTable)', 'error': f'{type(exc).__name__}: {exc}'})
        run = getattr(hwp, 'Run', None)
        if callable(run):
            try:
                raw = run('TableSplitTable')
                attempts.append({'method': 'Run(TableSplitTable)', 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__})
                return {'succeeded': raw is None or bool(raw), 'method': 'Run', 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'Run(TableSplitTable)', 'error': f'{type(exc).__name__}: {exc}'})
        py_method = getattr(hwp, 'TableSplitTable', None)
        if callable(py_method):
            try:
                raw = py_method()
                attempts.append({'method': 'TableSplitTable()', 'result': bool(raw) if raw is not None else None, 'raw_type': type(raw).__name__})
                return {'succeeded': raw is None or bool(raw), 'method': 'TableSplitTable', 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'TableSplitTable()', 'error': f'{type(exc).__name__}: {exc}'})
        return {'succeeded': False, 'method': None, 'attempts': attempts}

    def _bundle_table_split_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name='table_split_exact', require_table=True)
        down_rows = int(step.get('down_rows') or 0)
        max_controls = int(step.get('max_controls') or 2048)
        page_from = resolved.get('page_from')
        page_to = resolved.get('page_to') or page_from
        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_metrics = self._bundle_table_cell_metrics(hwp, resolved['target_ctrl'])
        before_tables = self._bundle_page_table_items(hwp, page_from=page_from, page_to=page_to, max_controls=max_controls)
        mutation_original_pos = None
        try:
            mutation_original_pos = _get_pos(hwp)
        except Exception:
            mutation_original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
            if not enter.get('is_cell') or not enter.get('normal_edit_state'):
                raise LocalCliRuntimeError('table_split_exact cannot enter the target table cell in normal edit state for mutation')
            edit_mode_normalization: list[dict[str, Any]] = []
            pre_navigation_state: dict[str, Any] = {'snapshot': self._bundle_compact_snapshot(hwp)}
            for key, method_name, kwargs in (
                ('cell_addr', 'get_cell_addr', {'as_': 'tuple'}),
                ('row_count', 'get_row_num', {}),
                ('row_height_hu', 'get_row_height', {'as_': 'hwpunit'}),
            ):
                method = getattr(hwp, method_name, None)
                if callable(method):
                    try:
                        pre_navigation_state[key] = method(**kwargs)
                    except Exception as exc:
                        pre_navigation_state[key] = {'error': f'{type(exc).__name__}: {exc}'}
                else:
                    pre_navigation_state[key] = {'unavailable': method_name}
            navigation: list[dict[str, Any]] = []
            lower = getattr(hwp, 'TableLowerCell', None)
            haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            for move_index in range(down_rows):
                before_cell = self._bundle_current_cell_addr_tuple(hwp)
                try:
                    moved_down = False
                    for attempt_index in range(3):
                        if callable(lower):
                            raw = lower()
                            method = 'TableLowerCell()'
                        elif callable(haction_run):
                            raw = haction_run('TableLowerCell')
                            method = 'HAction.Run(TableLowerCell)'
                        else:
                            raise LocalCliRuntimeError('TableLowerCell navigation is unavailable')
                        after_cell = self._bundle_current_cell_addr_tuple(hwp)
                        snapshot = self._bundle_compact_snapshot(hwp)
                        moved_down = (
                            before_cell is not None
                            and after_cell is not None
                            and int(after_cell[1]) > int(before_cell[1])
                        )
                        navigation.append({
                            'step': move_index + 1,
                            'attempt': attempt_index + 1,
                            'method': method,
                            'result': bool(raw) if raw is not None else None,
                            'before_cell': list(before_cell) if before_cell is not None else None,
                            'after_cell': list(after_cell) if after_cell is not None else None,
                            'moved_down': moved_down,
                            'snapshot': snapshot,
                        })
                        if moved_down:
                            break
                    if not moved_down:
                        raise LocalCliRuntimeError(f'table_split_exact failed to move down to split row at step {move_index + 1}; pre_navigation_state={pre_navigation_state!r}; navigation={navigation!r}')
                except Exception as exc:
                    navigation.append({'step': move_index + 1, 'error': f'{type(exc).__name__}: {exc}'})
                    raise
            split_cell_snapshot = self._bundle_compact_snapshot(hwp)
            if not split_cell_snapshot.get('is_cell'):
                raise LocalCliRuntimeError('table_split_exact split point is not inside a table cell after navigation')
            if split_cell_snapshot.get('has_selection') or int(split_cell_snapshot.get('selection_mode') or 0) != 0:
                text_box_edit = getattr(hwp, 'ShapeObjTextBoxEdit', None)
                if callable(text_box_edit):
                    try:
                        raw = text_box_edit()
                        normalized_snapshot = self._bundle_compact_snapshot(hwp)
                        edit_mode_normalization.append({'method': 'ShapeObjTextBoxEdit before TableSplitTable', 'result': bool(raw) if raw is not None else None, 'before': split_cell_snapshot, 'after': normalized_snapshot})
                        split_cell_snapshot = normalized_snapshot
                    except Exception as exc:
                        edit_mode_normalization.append({'method': 'ShapeObjTextBoxEdit before TableSplitTable', 'error': f'{type(exc).__name__}: {exc}', 'before': split_cell_snapshot})
                if split_cell_snapshot.get('has_selection') or int(split_cell_snapshot.get('selection_mode') or 0) != 0:
                    raise LocalCliRuntimeError('table_split_exact split point is not in normal edit state before TableSplitTable')
            split_result = self._run_table_split_action(hwp)
            if not split_result.get('succeeded'):
                raise LocalCliRuntimeError(f'table_split_exact TableSplitTable did not succeed: {split_result.get("attempts")!r}')
            after_snapshot = self._bundle_compact_snapshot(hwp)
            after_tables = self._bundle_page_table_items(hwp, page_from=page_from, page_to=page_to, max_controls=max_controls)
            changed = before_tables != after_tables
            if not changed:
                raise LocalCliRuntimeError('table_split_exact refused to mark success: post-split table inventory did not change')
            return {
                'schema_version': 'local-cli/table-split-exact/v1',
                'succeeded': True,
                'doc_backed_action': 'TableSplitTable',
                'doc_behavior': 'Hancom Split Table splits beneath the row at the cursor position; first-row split is invalid.',
                'enumeration_mode': resolved['enumeration_mode'],
                'scope': {
                    'section_anchor': resolved['section_anchor'],
                    'page_from': resolved['page_from'],
                    'page_to': resolved['page_to'],
                    'around': resolved['around'],
                },
                'target_proof': {
                    'target_id': resolved['target_id'],
                    'expected_hash': resolved['expected_hash'],
                    'expected_page': resolved['expected_page'],
                    'matched_before': resolved['before_item'],
                    'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
                },
                'before': before_snapshot,
                'before_metrics': before_metrics,
                'before_tables': before_tables,
                'enter': enter,
                'edit_mode_normalization': edit_mode_normalization,
                'down_rows': down_rows,
                'pre_navigation_state': pre_navigation_state,
                'navigation': navigation,
                'split_cell_snapshot': split_cell_snapshot,
                'split_result': split_result,
                'after': after_snapshot,
                'after_tables': after_tables,
                'changed': changed,
                'warnings': [],
            }
        finally:
            if mutation_original_pos is not None and len(mutation_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(mutation_original_pos[0]), int(mutation_original_pos[1]), int(mutation_original_pos[2]))
                except Exception:
                    pass

    def _bundle_cell_row_fit_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('cell_row_fit_exact requires confirm_layout=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('cell_row_fit_exact requires positive expected_page')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'cell_row_fit_exact section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'cell_row_fit_exact around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        if len(matching) != 1:
            raise LocalCliRuntimeError(f'cell_row_fit_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}')
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('type') != 'tbl':
            raise LocalCliRuntimeError(f"cell_row_fit_exact target must be a table control, got {before_item.get('type')!r}")
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(
                f"cell_row_fit_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}"
            )
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'cell_row_fit_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(f'cell_row_fit_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}')
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(f'cell_row_fit_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}')

        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
        if not before_metrics.get('available'):
            raise LocalCliRuntimeError(f'cell_row_fit_exact cannot read row metrics: {before_metrics.get("error")}')
        before_height_hu = float(before_metrics.get('row_height_hu'))
        new_height_hu = None
        if step.get('row_height_percent') is not None:
            new_height_hu = max(1, int(round(before_height_hu * float(step.get('row_height_percent')) / 100.0)))
        elif step.get('row_height_hu') is not None:
            new_height_hu = int(round(float(step.get('row_height_hu'))))
        elif step.get('row_height_mm') is not None:
            new_height_hu = self._mm_to_hwp_unit(hwp, float(step.get('row_height_mm')))
        elif step.get('resize_up_steps') is None and step.get('resize_down_steps') is None and step.get('line_spacing') is None and step.get('char_height_percent') is None:
            raise LocalCliRuntimeError('cell_row_fit_exact requires row_height_percent, row_height_hu, row_height_mm, resize steps, or contained style fit')
        if new_height_hu is not None and new_height_hu <= 0:
            raise LocalCliRuntimeError('cell_row_fit_exact computed non-positive row height')

        mutation_original_pos = None
        try:
            mutation_original_pos = _get_pos(hwp)
        except Exception:
            mutation_original_pos = None
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, target_ctrl)
            if not enter.get('is_cell'):
                raise LocalCliRuntimeError('cell_row_fit_exact cannot enter the target table cell for mutation')
            raw_results = []
            if new_height_hu is not None:
                set_row_height = getattr(hwp, 'set_row_height', None)
                if not callable(set_row_height):
                    raise LocalCliRuntimeError('cell_row_fit_exact requires pyhwpx set_row_height')
                raw_result = set_row_height(new_height_hu, as_='hwpunit')
                raw_results.append({'method': 'set_row_height', 'result': bool(raw_result) if raw_result is not None else None})
                op_name = 'set-row-height'
            elif step.get('line_spacing') is not None or step.get('char_height_percent') is not None:
                run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                if callable(run):
                    for action in ('TableCellBlock', 'TableCellBlockExtend'):
                        try:
                            raw = run(action)
                            raw_results.append({'method': action, 'result': bool(raw) if raw is not None else None})
                        except Exception as exc:
                            raw_results.append({'method': action, 'error': f'{type(exc).__name__}: {exc}'})
                if step.get('line_spacing') is not None:
                    set_para = getattr(hwp, 'set_para', None)
                    if not callable(set_para):
                        raise LocalCliRuntimeError('cell_row_fit_exact requires pyhwpx set_para for line_spacing')
                    raw = set_para(LineSpacing=int(step.get('line_spacing')))
                    raw_results.append({'method': 'set_para(LineSpacing)', 'value': int(step.get('line_spacing')), 'result': bool(raw) if raw is not None else None})
                if step.get('char_height_percent') is not None:
                    h_parameter_set = getattr(hwp, 'HParameterSet', None)
                    h_action = getattr(hwp, 'HAction', None)
                    if h_parameter_set is None or h_action is None:
                        raise LocalCliRuntimeError('cell_row_fit_exact requires HParameterSet/HAction for char height')
                    pset = h_parameter_set.HCharShape
                    h_action.GetDefault('CharShape', pset.HSet)
                    old_height = getattr(pset, 'Height')
                    new_height = max(100, int(round(float(old_height) * float(step.get('char_height_percent')) / 100.0)))
                    setattr(pset, 'Height', new_height)
                    raw = h_action.Execute('CharShape', pset.HSet)
                    raw_results.append({'method': 'CharShape.Height', 'old_height': old_height, 'new_height': new_height, 'percent': float(step.get('char_height_percent')), 'result': bool(raw) if raw is not None else None})
                op_name = 'contained-style-fit'
            else:
                action_name = 'TableResizeUp' if step.get('resize_up_steps') is not None else 'TableResizeDown'
                count = int(step.get('resize_up_steps') or step.get('resize_down_steps') or 0)
                run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                method = getattr(hwp, action_name, None)
                for _ in range(count):
                    if callable(method):
                        raw = method()
                    elif callable(run):
                        raw = run(action_name)
                    else:
                        raise LocalCliRuntimeError(f'cell_row_fit_exact requires {action_name}')
                    raw_results.append({'method': action_name, 'result': bool(raw) if raw is not None else None})
                op_name = action_name
            operation = {
                'op': op_name,
                'old_row_height_hu': before_height_hu,
                'old_row_height_mm': self._hwp_unit_to_mm(hwp, before_height_hu),
                'new_row_height_hu': new_height_hu,
                'new_row_height_mm': self._hwp_unit_to_mm(hwp, new_height_hu) if new_height_hu is not None else None,
                'row_height_percent': step.get('row_height_percent'),
                'row_height_hu': step.get('row_height_hu'),
                'row_height_mm': step.get('row_height_mm'),
                'resize_up_steps': step.get('resize_up_steps'),
                'resize_down_steps': step.get('resize_down_steps'),
                'line_spacing': step.get('line_spacing'),
                'char_height_percent': step.get('char_height_percent'),
                'raw_results': raw_results[:12],
                'raw_result_count': len(raw_results),
                'enter': enter,
            }
        except LocalCliRuntimeError:
            raise
        except Exception as exc:
            raise LocalCliRuntimeError(f'cell_row_fit_exact mutation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            if mutation_original_pos is not None and len(mutation_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(mutation_original_pos[0]), int(mutation_original_pos[1]), int(mutation_original_pos[2]))
                except Exception:
                    pass

        after_snapshot = self._bundle_compact_snapshot(hwp)
        after_metrics = self._bundle_table_cell_metrics(hwp, target_ctrl)
        before_after_delta = {
            key: {'before': before_metrics.get(key), 'after': after_metrics.get(key)}
            for key in sorted(set(before_metrics) | set(after_metrics))
            if before_metrics.get(key) != after_metrics.get(key)
        }
        style_selector_used = step.get('line_spacing') is not None or step.get('char_height_percent') is not None
        if style_selector_used:
            style_changed = any(
                before_metrics.get(key) != after_metrics.get(key)
                for key in ('char_height_raw', 'para_line_spacing', 'para_line_spacing_type')
            )
            if not style_changed:
                raise LocalCliRuntimeError(
                    'cell_row_fit_exact did not observe changed contained style metrics after mutation; '
                    f'before={before_metrics!r}; after={after_metrics!r}; operation={operation!r}'
                )
        elif before_metrics.get('row_height_hu') == after_metrics.get('row_height_hu'):
            raise LocalCliRuntimeError(
                'cell_row_fit_exact did not observe changed row_height_hu after mutation; '
                f'before={before_metrics!r}; after={after_metrics!r}; operation={operation!r}'
            )

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        if len(post_controls) != len(controls):
            raise LocalCliRuntimeError(f'cell_row_fit_exact control count changed unexpectedly: before={len(controls)}, after={len(post_controls)}')
        post_target_items: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    post_target_items.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if len(post_target_items) != 1:
            raise LocalCliRuntimeError(f'cell_row_fit_exact post-mutation target count must be exactly 1, got {len(post_target_items)} for {target_id!r}')
        return {
            'schema_version': 'local-cli/cell-row-fit-exact/v1',
            'read_only': False,
            'mutation': 'cell-row-fit',
            'succeeded': True,
            'enumeration_mode': enumeration_mode,
            'scope': {'section_anchor': section_anchor, 'page_from': page_from, 'page_to': page_to, 'around': around},
            'anchors': {'section_anchor': anchor_evidence, 'around': around_evidence},
            'target_proof': {
                'target_id': target_id,
                'expected_hash': expected_hash,
                'expected_page': expected_page,
                'matched_before': before_item,
                'matched_after': post_target_items[0],
                'target_anchor_pos': list(target_anchor_pos) if target_anchor_pos is not None else None,
            },
            'operation': operation,
            'before': before_snapshot,
            'after': after_snapshot,
            'metrics_before': before_metrics,
            'metrics_after': after_metrics,
            'changed_metrics': before_after_delta,
            'post_mutation': {'control_count_before': len(controls), 'control_count_after': len(post_controls), 'post_target_count': len(post_target_items)},
            'warnings': ['This primitive mutates one target table row/cell height only; rendered before/after proof is required before accepting the working copy.'],
        }

    def _bundle_control_move_resize_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('control_move_resize_exact requires confirm_layout=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('control_move_resize_exact requires positive expected_page')

        scale_percent = step.get('scale_percent')
        scale_factor = float(scale_percent) / 100.0 if scale_percent is not None else None
        move_dx_mm = float(step.get('move_dx_mm') or 0.0)
        move_dy_mm = float(step.get('move_dy_mm') or 0.0)
        if scale_factor is None and move_dx_mm == 0.0 and move_dy_mm == 0.0:
            raise LocalCliRuntimeError('control_move_resize_exact requires scale_percent or non-zero move delta')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_move_resize_exact section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_move_resize_exact around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        if len(matching) != 1:
            raise LocalCliRuntimeError(
                f'control_move_resize_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}'
            )
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(
                f"control_move_resize_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}"
            )
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'control_move_resize_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(
                f'control_move_resize_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}'
            )
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(
                f'control_move_resize_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}'
            )

        before_snapshot = self._bundle_compact_snapshot(hwp)
        before_shape = self._shape_prop_snapshot(target_ctrl)
        if not before_shape.get('available'):
            raise LocalCliRuntimeError(f'control_move_resize_exact cannot read shape properties: {before_shape.get("error")}')
        before_values = dict(before_shape.get('values') or {})
        if 'Width' not in before_values or 'Height' not in before_values:
            raise LocalCliRuntimeError('control_move_resize_exact requires readable Width and Height shape properties')

        selection_proof = self._bundle_select_control_exact(hwp, target_ctrl, before_item)
        selection_warnings: list[str] = []
        if not selection_proof.get('selection_succeeded'):
            selection_warnings.append('Native exact control selection was unavailable; fell back to pre-existing inventory target proof and direct Ctrl.Properties mutation.')
        elif selection_proof.get('proof_strength') != 'exact-ctrl-inst-id':
            selection_warnings.append('CtrlInstID exact selection was unavailable or unverified; fallback selected-control proof was used before direct Ctrl.Properties mutation.')

        try:
            prop = getattr(target_ctrl, 'Properties')
            operations: list[dict[str, Any]] = []
            if scale_factor is not None:
                old_width = float(before_values['Width'])
                old_height = float(before_values['Height'])
                new_width = max(1, int(round(old_width * scale_factor)))
                new_height = max(1, int(round(old_height * scale_factor)))
                self._shape_prop_set_item(prop, 'Width', new_width)
                self._shape_prop_set_item(prop, 'Height', new_height)
                operations.append({'op': 'scale', 'scale_percent': float(scale_percent), 'old_width': old_width, 'old_height': old_height, 'new_width': new_width, 'new_height': new_height})
            if move_dx_mm != 0.0:
                old = self._shape_prop_item(prop, 'HorzOffset')
                if old is None:
                    raise LocalCliRuntimeError('control_move_resize_exact cannot move dx: HorzOffset is unavailable')
                delta = self._mm_to_hwp_unit(hwp, move_dx_mm)
                self._shape_prop_set_item(prop, 'HorzOffset', int(round(float(old))) + delta)
                operations.append({'op': 'move-dx', 'move_dx_mm': move_dx_mm, 'delta_hwpunit': delta, 'old': old, 'new': int(round(float(old))) + delta})
            if move_dy_mm != 0.0:
                old = self._shape_prop_item(prop, 'VertOffset')
                if old is None:
                    raise LocalCliRuntimeError('control_move_resize_exact cannot move dy: VertOffset is unavailable')
                delta = self._mm_to_hwp_unit(hwp, move_dy_mm)
                self._shape_prop_set_item(prop, 'VertOffset', int(round(float(old))) + delta)
                operations.append({'op': 'move-dy', 'move_dy_mm': move_dy_mm, 'delta_hwpunit': delta, 'old': old, 'new': int(round(float(old))) + delta})
            target_ctrl.Properties = prop
        except LocalCliRuntimeError:
            raise
        except Exception as exc:
            raise LocalCliRuntimeError(f'control_move_resize_exact property mutation failed: {type(exc).__name__}: {exc}') from exc

        after_snapshot = self._bundle_compact_snapshot(hwp)
        after_shape = self._shape_prop_snapshot(target_ctrl)
        changed_values = {
            key: {'before': before_values.get(key), 'after': (after_shape.get('values') or {}).get(key)}
            for key in sorted(set(before_values) | set((after_shape.get('values') or {}).keys()))
            if before_values.get(key) != (after_shape.get('values') or {}).get(key)
        }

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        if len(post_controls) != len(controls):
            raise LocalCliRuntimeError(
                f'control_move_resize_exact control count changed unexpectedly: before={len(controls)}, after={len(post_controls)}'
            )
        post_target_items: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    post_target_items.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if len(post_target_items) != 1:
            raise LocalCliRuntimeError(
                f'control_move_resize_exact post-mutation target count must be exactly 1, got {len(post_target_items)} for {target_id!r}'
            )
        if not changed_values:
            raise LocalCliRuntimeError('control_move_resize_exact did not observe any changed shape property after mutation')

        return {
            'schema_version': 'local-cli/control-move-resize-exact/v1',
            'read_only': False,
            'mutation': 'move-resize-control',
            'succeeded': True,
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': section_anchor,
                'page_from': page_from,
                'page_to': page_to,
                'around': around,
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'target_proof': {
                'target_id': target_id,
                'expected_hash': expected_hash,
                'expected_page': expected_page,
                'matched_before': before_item,
                'matched_after': post_target_items[0],
                'target_anchor_pos': list(target_anchor_pos) if target_anchor_pos is not None else None,
            },
            'operation_request': {
                'scale_percent': scale_percent,
                'move_dx_mm': move_dx_mm,
                'move_dy_mm': move_dy_mm,
            },
            'selection_proof': selection_proof,
            'selection_method_used': selection_proof.get('method_used'),
            'operations': operations,
            'before': before_snapshot,
            'after': after_snapshot,
            'shape_before': before_shape,
            'shape_after': after_shape,
            'changed_values': changed_values,
            'post_mutation': {
                'control_count_before': len(controls),
                'control_count_after': len(post_controls),
                'post_target_count': len(post_target_items),
            },
            'warnings': [
                *selection_warnings,
                'This primitive mutates one control layout only; rendered before/after proof is required before accepting the working copy.',
            ],
        }

    def _bundle_paragraph_rehome_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        delete_match = self._bundle_require_text(step, 'delete_match', max_chars=1000)
        insert_before_match = self._bundle_require_text(step, 'insert_before_match', max_chars=1000)
        insert_text = self._bundle_require_text(step, 'insert_text', max_chars=1000)
        delete_expected_page = int(step.get('delete_expected_page') or 0)
        insert_expected_page = int(step.get('insert_expected_page') or 0)
        if delete_expected_page <= 0 or insert_expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_rehome_exact requires positive expected pages')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_rehome_exact requires confirm_layout=true')
        if _remove_visible_spaces(delete_match) != _remove_visible_spaces(insert_text):
            raise LocalCliRuntimeError('paragraph_rehome_exact requires delete_match and insert_text to preserve visible text')

        before_text = ''
        try:
            if hasattr(hwp, 'get_text_file'):
                before_text = str(hwp.get_text_file('UNICODE', '') or '')
            elif hasattr(hwp, 'GetTextFile'):
                before_text = str(hwp.GetTextFile('UNICODE', '') or '')
        except Exception:
            before_text = ''
        before_visible_no_space = _remove_visible_spaces(before_text)
        before_delete_count = before_text.count(delete_match)
        before_insert_anchor_count = before_text.count(insert_before_match)

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            insert_target = self._find_text_on_expected_page(hwp, match=insert_before_match, expected_page=insert_expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_rehome_exact expected active selection for insert target')
            _, slist, spara, spos, _elist, _epara, _epos = selected
            _set_pos(hwp, int(slist), int(spara), int(spos))
            insert_context_before = _capture_nearby_text_context(hwp)
            insert_text_at_caret(hwp, insert_text)
            insert_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            raw_line_break = None
            if callable(insert_run):
                raw_line_break = insert_run('BreakLine')
            else:
                raise LocalCliRuntimeError('paragraph_rehome_exact requires HAction.Run BreakLine after insertion')

            delete_target = self._find_text_on_expected_page(hwp, match=delete_match, expected_page=delete_expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_rehome_exact expected active selection for delete target')
            selected_text = _get_selected_text(hwp, keep_select=True)
            if _remove_visible_spaces(selected_text) != _remove_visible_spaces(delete_match):
                raise LocalCliRuntimeError(f'paragraph_rehome_exact selected delete text mismatch: {selected_text!r}')
            _, _dslist, _dspara, _dspos, del_elist, del_epara, del_epos = selected
            _set_pos(hwp, int(del_elist), int(del_epara), int(del_epos))
            delete_context_before = _capture_nearby_text_context(hwp)
            delete_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if not callable(delete_run):
                raise LocalCliRuntimeError('paragraph_rehome_exact requires HAction.Run DeleteBack for exact text deletion')
            raw_delete_results = []
            for _i in range(len(delete_match)):
                raw_delete_results.append(delete_run('DeleteBack'))
            raw_delete = all(bool(item) if item is not None else True for item in raw_delete_results)

            after_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    after_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    after_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                after_text = ''
            after_visible_no_space = _remove_visible_spaces(after_text)
            if before_visible_no_space and after_visible_no_space != before_visible_no_space:
                try:
                    undo = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                    if callable(undo):
                        undo('Undo')
                        undo('Undo')
                except Exception:
                    pass
                diff_at = next((i for i, (a, b) in enumerate(zip(before_visible_no_space, after_visible_no_space)) if a != b), min(len(before_visible_no_space), len(after_visible_no_space)))
                before_frag = before_visible_no_space[max(0, diff_at-40):diff_at+80]
                after_frag = after_visible_no_space[max(0, diff_at-40):diff_at+80]
                raise LocalCliRuntimeError(
                    'paragraph_rehome_exact changed visible text; undo attempted and mutation refused; '
                    f'before_len={len(before_visible_no_space)} after_len={len(after_visible_no_space)} diff_at={diff_at} '
                    f'before_frag={before_frag!r} after_frag={after_frag!r}'
                )
            after_delete_count = after_text.count(delete_match)
            after_insert_anchor_count = after_text.count(insert_before_match)
            return {
                'schema_version': 'local-cli/paragraph-rehome-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-rehome',
                'delete_match_preview': _preview_text(delete_match, limit=80),
                'insert_before_match_preview': _preview_text(insert_before_match, limit=80),
                'delete_expected_page': delete_expected_page,
                'insert_expected_page': insert_expected_page,
                'insert_target': insert_target,
                'delete_target': delete_target,
                'insert_context_before': insert_context_before,
                'delete_context_before': delete_context_before,
                'raw_line_break_result': bool(raw_line_break) if raw_line_break is not None else None,
                'raw_delete_result': bool(raw_delete) if raw_delete is not None else None,
                'raw_delete_count': len(delete_match),
                'text_counts': {
                    'delete_match_before': before_delete_count,
                    'delete_match_after': after_delete_count,
                    'insert_before_match_before': before_insert_anchor_count,
                    'insert_before_match_after': after_insert_anchor_count,
                },
                'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
                'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
                'warnings': ['Rendered proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_control_join_previous_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        delete_back_count = int(step.get('delete_back_count') or 1)
        max_page_after = int(step.get('max_page_after') or max(1, expected_page - 1))
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('control_join_previous_exact requires confirm_layout=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('control_join_previous_exact requires positive expected_page')
        if not (1 <= delete_back_count <= 5):
            raise LocalCliRuntimeError('control_join_previous_exact delete_back_count must be 1..5')

        before_text = ''
        try:
            if hasattr(hwp, 'get_text_file'):
                before_text = str(hwp.get_text_file('UNICODE', '') or '')
            elif hasattr(hwp, 'GetTextFile'):
                before_text = str(hwp.GetTextFile('UNICODE', '') or '')
        except Exception:
            before_text = ''
        before_visible_no_space = _remove_visible_spaces(before_text)

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
        if len(matching) != 1:
            raise LocalCliRuntimeError(f'control_join_previous_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}')
        _target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(f"control_join_previous_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}")
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'control_join_previous_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(f'control_join_previous_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}')
        if page_from is not None and not (int(page_from) <= int(actual_page) <= int(page_to)):
            raise LocalCliRuntimeError(f'control_join_previous_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}')
        if target_anchor_pos is None:
            raise LocalCliRuntimeError(f'control_join_previous_exact cannot prove anchor position for {target_id!r}')

        before_snapshot = self._bundle_compact_snapshot(hwp)
        _set_pos(hwp, int(target_anchor_pos[0]), int(target_anchor_pos[1]), int(target_anchor_pos[2]))
        context_before = _capture_nearby_text_context(hwp)
        run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        if not callable(run):
            raise LocalCliRuntimeError('control_join_previous_exact requires HAction.Run')
        raw_results = []
        for _i in range(delete_back_count):
            raw_results.append(run('DeleteBack'))

        after_text = ''
        try:
            if hasattr(hwp, 'get_text_file'):
                after_text = str(hwp.get_text_file('UNICODE', '') or '')
            elif hasattr(hwp, 'GetTextFile'):
                after_text = str(hwp.GetTextFile('UNICODE', '') or '')
        except Exception:
            after_text = ''
        after_visible_no_space = _remove_visible_spaces(after_text)
        if before_visible_no_space and after_visible_no_space != before_visible_no_space:
            try:
                for _i in range(delete_back_count):
                    run('Undo')
            except Exception:
                pass
            diff_at = next((i for i, (a, b) in enumerate(zip(before_visible_no_space, after_visible_no_space)) if a != b), min(len(before_visible_no_space), len(after_visible_no_space)))
            before_frag = before_visible_no_space[max(0, diff_at-40):diff_at+80]
            after_frag = after_visible_no_space[max(0, diff_at-40):diff_at+80]
            raise LocalCliRuntimeError(
                'control_join_previous_exact changed visible text; undo attempted and mutation refused; '
                f'before_len={len(before_visible_no_space)} after_len={len(after_visible_no_space)} diff_at={diff_at} '
                f'before_frag={before_frag!r} after_frag={after_frag!r}'
            )

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        if len(post_controls) != len(controls):
            raise LocalCliRuntimeError(f'control_join_previous_exact control count changed unexpectedly: before={len(controls)}, after={len(post_controls)}')
        post_items: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('target_id') == target_id:
                    post_items.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if len(post_items) != 1:
            raise LocalCliRuntimeError(f'control_join_previous_exact post-mutation target count must be exactly 1, got {len(post_items)} for {target_id!r}')
        after_item = post_items[0]
        after_page = after_item.get('page')
        if after_page is None or int(after_page) > max_page_after:
            raise LocalCliRuntimeError(f'control_join_previous_exact target page after mutation exceeds max_page_after={max_page_after}: {after_page}')
        after_snapshot = self._bundle_compact_snapshot(hwp)
        return {
            'schema_version': 'local-cli/control-join-previous-exact/v1',
            'read_only': False,
            'mutation': 'control-join-previous',
            'enumeration_mode': enumeration_mode,
            'target_id': target_id,
            'expected_hash': expected_hash,
            'expected_page': expected_page,
            'delete_back_count': delete_back_count,
            'max_page_after': max_page_after,
            'target_anchor_pos': list(target_anchor_pos),
            'target_before': before_item,
            'target_after': after_item,
            'context_before': context_before,
            'before': before_snapshot,
            'after': after_snapshot,
            'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
            'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
            'raw_results': [bool(item) if item is not None else None for item in raw_results],
            'warnings': ['Rendered before/after proof is required before accepting the working copy.'],
        }

    def _bundle_control_delete_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        target_id = self._bundle_require_text(step, 'target_id', max_chars=500)
        expected_hash = self._bundle_require_text(step, 'expected_hash', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        page_from = step.get('page_from')
        page_to = step.get('page_to') or page_from
        max_controls = int(step.get('max_controls') or 2048)
        if not bool(step.get('confirm_remove')):
            raise LocalCliRuntimeError('control_delete_exact requires confirm_remove=true')
        if expected_page <= 0:
            raise LocalCliRuntimeError('control_delete_exact requires positive expected_page')

        section_anchor = str(step.get('section_anchor') or '').strip() or None
        around = str(step.get('around') or '').strip() or None
        anchor_evidence = self._bundle_find_anchor_evidence(hwp, section_anchor)
        around_evidence = self._bundle_find_anchor_evidence(hwp, around)
        if section_anchor and not (isinstance(anchor_evidence, dict) and anchor_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_delete_exact section_anchor not found: {section_anchor!r}')
        if around and not (isinstance(around_evidence, dict) and around_evidence.get('found')):
            raise LocalCliRuntimeError(f'control_delete_exact around anchor not found: {around!r}')

        controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None

        matching: list[tuple[Any, dict[str, Any], tuple[int, int, int] | None]] = []
        proof_items: list[dict[str, Any]] = []
        try:
            for index, ctrl in enumerate(controls):
                item, _snapshot, anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                proof_items.append(item)
                if item.get('target_id') == target_id:
                    matching.append((ctrl, item, anchor_pos))
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

        target_id_matches = [item for _ctrl, item, _anchor_pos in matching]
        if len(matching) != 1:
            raise LocalCliRuntimeError(
                f'control_delete_exact target_id match count must be exactly 1, got {len(matching)} for {target_id!r}'
            )
        target_ctrl, before_item, target_anchor_pos = matching[0]
        if before_item.get('proof_hash') != expected_hash:
            raise LocalCliRuntimeError(
                f"control_delete_exact proof_hash mismatch for {target_id!r}: expected {expected_hash!r}, got {before_item.get('proof_hash')!r}"
            )
        actual_page = before_item.get('page')
        if actual_page is None:
            raise LocalCliRuntimeError(f'control_delete_exact cannot prove page for {target_id!r}; refusing mutation')
        if int(actual_page) != expected_page:
            raise LocalCliRuntimeError(
                f'control_delete_exact expected_page mismatch for {target_id!r}: expected {expected_page}, got {actual_page}'
            )
        if page_from is not None:
            if not (int(page_from) <= int(actual_page) <= int(page_to)):
                raise LocalCliRuntimeError(
                    f'control_delete_exact page/scope mismatch: target page {actual_page} outside {page_from}-{page_to}'
                )

        before_snapshot = self._bundle_compact_snapshot(hwp)
        if target_anchor_pos is not None:
            _set_pos(hwp, target_anchor_pos[0], target_anchor_pos[1], target_anchor_pos[2])
        delete_succeeded = _delete_ctrl(hwp, target_ctrl)
        after_snapshot = self._bundle_compact_snapshot(hwp)

        post_controls, _post_mode = _enumerate_controls_headctrl(hwp, max_controls=max_controls)
        remaining_same_hash: list[dict[str, Any]] = []
        remaining_same_id: list[dict[str, Any]] = []
        post_original_pos = None
        try:
            post_original_pos = _get_pos(hwp)
        except Exception:
            post_original_pos = None
        try:
            for index, ctrl in enumerate(post_controls):
                item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
                if item.get('proof_hash') == expected_hash:
                    remaining_same_hash.append(item)
                if item.get('target_id') == target_id:
                    remaining_same_id.append(item)
        finally:
            if post_original_pos is not None and len(post_original_pos) >= 3:
                try:
                    _set_pos(hwp, int(post_original_pos[0]), int(post_original_pos[1]), int(post_original_pos[2]))
                except Exception:
                    pass
        if remaining_same_hash:
            raise LocalCliRuntimeError(
                f'control_delete_exact proof_hash still present after delete; remaining={remaining_same_hash}'
            )
        if len(post_controls) != len(controls) - 1:
            raise LocalCliRuntimeError(
                f'control_delete_exact control count did not decrease by one: before={len(controls)}, after={len(post_controls)}'
            )

        return {
            'schema_version': 'local-cli/control-delete-exact/v1',
            'read_only': False,
            'mutation': 'delete-control',
            'succeeded': bool(delete_succeeded),
            'enumeration_mode': enumeration_mode,
            'scope': {
                'section_anchor': section_anchor,
                'page_from': page_from,
                'page_to': page_to,
                'around': around,
            },
            'anchors': {
                'section_anchor': anchor_evidence,
                'around': around_evidence,
            },
            'target_proof': {
                'target_id': target_id,
                'expected_hash': expected_hash,
                'expected_page': expected_page,
                'matched_before': before_item,
                'target_id_match_count': len(target_id_matches),
                'target_anchor_pos': list(target_anchor_pos) if target_anchor_pos is not None else None,
            },
            'before': before_snapshot,
            'after': after_snapshot,
            'post_delete': {
                'control_count_before': len(controls),
                'control_count_after': len(post_controls),
                'remaining_same_hash_count': len(remaining_same_hash),
                'remaining_same_id_count': len(remaining_same_id),
                'remaining_same_id': remaining_same_id[:5],
            },
            'warnings': [
                'This primitive is destructive; safety relies on disposable/working-copy use and rendered before/after proof.',
            ],
        }

    def _style_parameter_snapshot(self, hwp: Any, action_name: str, set_name: str, keys: tuple[str, ...]) -> dict[str, Any]:
        result: dict[str, Any] = {'available': False, 'values': {}, 'method': None, 'error': None}
        haction = getattr(hwp, 'HAction', None)
        hparameter_set = getattr(hwp, 'HParameterSet', None)
        pset = getattr(hparameter_set, set_name, None) if hparameter_set is not None else None
        hset = getattr(pset, 'HSet', None) if pset is not None else None
        get_default = getattr(haction, 'GetDefault', None) if haction is not None else None
        try:
            if callable(get_default) and pset is not None and hset is not None:
                get_default(action_name, hset)
                result['available'] = True
                result['method'] = f'HAction.GetDefault({action_name}, HParameterSet.{set_name})'
            else:
                result['error'] = f'{action_name}/{set_name} parameter set unavailable'
                return result
            values: dict[str, Any] = {}
            for key in keys:
                try:
                    value = getattr(pset, key, None)
                except Exception as exc:
                    values[key] = f'<error: {exc}>'
                    continue
                if value is None or isinstance(value, (str, int, float, bool)):
                    values[key] = value
                else:
                    values[key] = str(value)
            result['values'] = values
            return result
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            return result

    def _bundle_style_inspect(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = str(step.get('match') or '').strip() or None
        keep_position = bool(step.get('keep_position'))
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        match_evidence: dict[str, Any] | None = None
        warnings: list[str] = []
        try:
            if match:
                _move_doc_begin(hwp)
                found = False
                find_method = getattr(hwp, 'find', None)
                if callable(find_method):
                    try:
                        found = bool(find_method(match, direction='Forward', MatchCase=1, WholeWordOnly=0))
                    except TypeError:
                        found = bool(find_method(match))
                if not found:
                    raise LocalCliRuntimeError(f'style-inspect match not found: {match!r}')
                snapshot = _snapshot_cursor_context(hwp)
                match_evidence = {
                    'found': True,
                    'pos': snapshot.get('pos'),
                    'selection_mode': snapshot.get('selection_mode'),
                    'current_paragraph_preview': snapshot.get('current_paragraph_preview'),
                    'page': self._bundle_page_evidence(hwp).get('page'),
                }

            location = snapshot_live_location(
                hwp=hwp,
                source_filename='style-inspect',
                working_copy_id='',
            )
            char_raw = self._style_parameter_snapshot(
                hwp,
                'CharShape',
                'HCharShape',
                ('Height', 'FaceNameHangul', 'FaceNameLatin', 'FaceNameHanja', 'FaceNameJapanese', 'FaceNameOther', 'Bold', 'Italic', 'Underline'),
            )
            para_raw = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('AlignType', 'LeftMargin', 'RightMargin', 'Indent', 'LineSpacing', 'LineSpacingType', 'PrevSpacing', 'NextSpacing'),
            )
            char_values = char_raw.get('values') if isinstance(char_raw.get('values'), dict) else {}
            para_values = para_raw.get('values') if isinstance(para_raw.get('values'), dict) else {}
            face_name = char_values.get('FaceNameHangul') or char_values.get('FaceNameLatin')
            height = char_values.get('Height')
            font_size_pt = None
            if isinstance(height, (int, float)):
                # Hancom commonly stores char height in 1/100 pt; keep raw too.
                font_size_pt = round(float(height) / 100.0, 2) if float(height) > 100 else float(height)
            if not char_raw.get('available'):
                warnings.append(str(char_raw.get('error') or 'character style unavailable'))
            if not para_raw.get('available'):
                warnings.append(str(para_raw.get('error') or 'paragraph style unavailable'))
            return {
                'schema_version': 'local-cli/style-inspect/v1',
                'read_only': True,
                'match': match,
                'match_evidence': match_evidence,
                'position': self._bundle_compact_location(location),
                'selection': {
                    'selection_summary': location.get('selection_summary'),
                    'has_selection': location.get('has_selection'),
                },
                'character': {
                    'font_size_pt': font_size_pt,
                    'height_raw': height,
                    'face_name': face_name,
                    'bold': char_values.get('Bold'),
                },
                'paragraph': {
                    'align': para_values.get('AlignType'),
                    'left_margin': para_values.get('LeftMargin'),
                    'indent': para_values.get('Indent'),
                    'line_spacing': para_values.get('LineSpacing'),
                    'line_spacing_type': para_values.get('LineSpacingType'),
                },
                'list': {
                    'enabled': None,
                    'kind': None,
                    'level': None,
                    'marker': None,
                    'note': 'list/bullet marker state is best-effort and not exposed by this first-pass primitive',
                },
                'raw': {
                    'char_shape': char_raw,
                    'para_shape': para_raw,
                },
                'warnings': warnings,
            }
        finally:
            if match and not keep_position and original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _find_text_on_expected_page(self, hwp: Any, *, match: str, expected_page: int) -> dict[str, Any]:
        _move_doc_begin(hwp)
        find_method = getattr(hwp, 'find', None)
        if not callable(find_method):
            raise LocalCliRuntimeError('exact paragraph primitive requires hwp.find')
        seen_positions: set[tuple[int, int, int]] = set()
        candidates: list[dict[str, Any]] = []
        for _ in range(200):
            try:
                found = bool(find_method(match, direction='Forward', MatchCase=1, WholeWordOnly=0))
            except TypeError:
                found = bool(find_method(match))
            if not found:
                break
            snapshot = _snapshot_cursor_context(hwp)
            raw_pos = snapshot.get('pos') or []
            try:
                pos_key = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
            except Exception:
                pos_key = (len(seen_positions), -1, -1)
            if pos_key in seen_positions:
                break
            seen_positions.add(pos_key)
            page = self._bundle_page_evidence(hwp).get('page')
            candidates.append({'page': page, 'pos': list(pos_key), 'snapshot': snapshot})
            if page is not None and int(page) == int(expected_page):
                return {'page': page, 'pos': list(pos_key), 'snapshot': snapshot, 'candidates': candidates}
        pages = [item.get('page') for item in candidates]
        raise LocalCliRuntimeError(f'exact paragraph primitive could not find {match!r} on expected_page={expected_page}; candidate_pages={pages!r}')

    def _find_paragraph_delete_target(
        self,
        hwp: Any,
        *,
        match: str,
        expected_page: int,
        occurrence_on_page: int,
        expected_previous_contains: str | None,
        expected_next_contains: str | None,
    ) -> dict[str, Any]:
        _move_doc_begin(hwp)
        find_method = getattr(hwp, 'find', None)
        if not callable(find_method):
            raise LocalCliRuntimeError('paragraph_delete_exact requires hwp.find')
        seen_positions: set[tuple[int, int, int]] = set()
        candidates: list[dict[str, Any]] = []
        matches_on_page: list[dict[str, Any]] = []
        for _ in range(500):
            try:
                found = bool(find_method(match, direction='Forward', MatchCase=1, WholeWordOnly=0))
            except TypeError:
                found = bool(find_method(match))
            if not found:
                break
            snapshot = _snapshot_cursor_context(hwp)
            raw_pos = snapshot.get('pos') or []
            try:
                pos_key = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
            except Exception:
                pos_key = (len(seen_positions), -1, -1)
            if pos_key in seen_positions:
                break
            seen_positions.add(pos_key)
            page = self._bundle_page_evidence(hwp).get('page')
            context = _capture_nearby_text_context(hwp)
            selected_text = ''
            try:
                selected_text = _get_selected_text(hwp, keep_select=True)
            except Exception:
                selected_text = ''
            item = {
                'page': page,
                'pos': list(pos_key),
                'snapshot': snapshot,
                'context': context,
                'selected_text_preview': _preview_text(selected_text, limit=120),
                'selected_text_hash': self._text_proof_hash(selected_text),
            }
            candidates.append(item)
            if page is None or int(page) != int(expected_page):
                continue
            previous_preview = str(context.get('previous_paragraph_preview') or '')
            next_preview = str(context.get('next_paragraph_preview') or '')
            if expected_previous_contains and expected_previous_contains not in previous_preview:
                continue
            if expected_next_contains and expected_next_contains not in next_preview:
                continue
            matches_on_page.append(item)
            if len(matches_on_page) == occurrence_on_page:
                return {**item, 'candidates': candidates, 'filtered_match_count': len(matches_on_page)}
        pages = [item.get('page') for item in candidates]
        contexts = [
            {
                'page': item.get('page'),
                'pos': item.get('pos'),
                'previous': (item.get('context') or {}).get('previous_paragraph_preview'),
                'current': (item.get('context') or {}).get('current_paragraph_preview'),
                'next': (item.get('context') or {}).get('next_paragraph_preview'),
            }
            for item in candidates[:20]
        ]
        raise LocalCliRuntimeError(
            f'paragraph_delete_exact could not find occurrence_on_page={occurrence_on_page} for {match!r} '
            f'on expected_page={expected_page}; candidate_pages={pages!r}; contexts={contexts!r}'
        )

    def _bundle_paragraph_delete_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_delete_exact requires positive expected_page')
        if not bool(step.get('confirm_remove')):
            raise LocalCliRuntimeError('paragraph_delete_exact requires confirm_remove=true')
        occurrence_on_page = int(step.get('occurrence_on_page') or 1)
        expected_previous_contains = str(step.get('expected_previous_contains') or '').strip() or None
        expected_next_contains = str(step.get('expected_next_contains') or '').strip() or None
        max_page_after = int(step.get('max_page_after') or expected_page)

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            before_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    before_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    before_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                before_text = ''
            before_lines = [_normalize_visible_text(line) for line in re.split(r'[\r\n]+', before_text)]
            before_exact_line_count = sum(1 for line in before_lines if line == _normalize_visible_text(match))
            if before_exact_line_count <= 0:
                raise LocalCliRuntimeError(
                    f'paragraph_delete_exact could not prove an exact visible line {match!r} before mutation; refusing cleanup'
                )

            target = self._find_paragraph_delete_target(
                hwp,
                match=match,
                expected_page=expected_page,
                occurrence_on_page=occurrence_on_page,
                expected_previous_contains=expected_previous_contains,
                expected_next_contains=expected_next_contains,
            )
            context_before = _capture_nearby_text_context(hwp)
            _select_paragraph_with_trailing_break_for_current_selection(hwp)
            selected_range = _get_selected_pos(hwp)
            selected_text = _get_selected_text(hwp, keep_select=True)
            selected_normalized = _normalize_visible_text(selected_text)
            if _normalize_visible_text(match) not in selected_normalized:
                raise LocalCliRuntimeError(
                    'paragraph_delete_exact selected text does not contain the target match; '
                    f'match={match!r}; selected={_preview_text(selected_text, limit=160)!r}; range={selected_range!r}'
                )
            if len(selected_text) > 5000:
                raise LocalCliRuntimeError('paragraph_delete_exact selected more than 5,000 chars; refusing possible overselection')
            _delete_selection(hwp)

            after_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    after_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    after_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                after_text = ''
            after_lines = [_normalize_visible_text(line) for line in re.split(r'[\r\n]+', after_text)]
            after_exact_line_count = sum(1 for line in after_lines if line == _normalize_visible_text(match))
            if after_exact_line_count != max(0, before_exact_line_count - 1):
                undo = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                try:
                    if callable(undo):
                        undo('Undo')
                except Exception:
                    pass
                raise LocalCliRuntimeError(
                    'paragraph_delete_exact post-proof failed: exact visible line count did not decrease by one; '
                    f'before={before_exact_line_count} after={after_exact_line_count}; undo attempted'
                )
            if expected_next_contains:
                after_next_target = self._find_text_on_expected_page(hwp, match=expected_next_contains, expected_page=min(max_page_after, expected_page))
                if after_next_target.get('page') is None or int(after_next_target.get('page')) > max_page_after:
                    raise LocalCliRuntimeError(
                        f'paragraph_delete_exact next guard exceeded max_page_after={max_page_after}: {after_next_target.get("page")}'
                    )
            context_after = _capture_nearby_text_context(hwp)
            return {
                'schema_version': 'local-cli/paragraph-delete-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-delete-exact',
                'match': match,
                'expected_page': expected_page,
                'occurrence_on_page': occurrence_on_page,
                'target_before': target,
                'context_before': context_before,
                'context_after': context_after,
                'selected_range': list(selected_range) if isinstance(selected_range, (list, tuple)) else selected_range,
                'selected_text_preview': _preview_text(selected_text, limit=160),
                'selected_text_hash': self._text_proof_hash(selected_text),
                'exact_line_count_before': before_exact_line_count,
                'exact_line_count_after': after_exact_line_count,
                'visible_text_hash_before': self._text_proof_hash(before_text),
                'visible_text_hash_after': self._text_proof_hash(after_text),
                'warnings': ['Destructive paragraph removal; rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_paragraph_join_previous_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_join_previous_exact requires positive expected_page')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_join_previous_exact requires confirm_layout=true')
        delete_back_count = int(step.get('delete_back_count') or 1)
        if not (1 <= delete_back_count <= 5):
            raise LocalCliRuntimeError('paragraph_join_previous_exact delete_back_count must be 1..5')
        max_page_after = int(step.get('max_page_after') or expected_page)
        expected_previous_contains = str(step.get('expected_previous_contains') or '').strip() or None

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            before_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    before_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    before_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                before_text = ''
            before_visible_no_space = _remove_visible_spaces(before_text)

            target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_join_previous_exact expected active selection after find')
            _, slist, spara, spos, _elist, _epara, _epos = selected
            _set_pos(hwp, int(slist), int(spara), 0)
            context_before = _capture_nearby_text_context(hwp)
            if expected_previous_contains:
                previous_preview = str(context_before.get('previous_paragraph_preview') or '')
                if expected_previous_contains not in previous_preview:
                    raise LocalCliRuntimeError(
                        f'paragraph_join_previous_exact previous paragraph guard failed: expected {expected_previous_contains!r}; got {previous_preview!r}'
                    )

            run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if not callable(run):
                raise LocalCliRuntimeError('paragraph_join_previous_exact requires HAction.Run')
            raw_results = []
            for _i in range(delete_back_count):
                raw_results.append(run('DeleteBack'))

            after_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    after_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    after_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                after_text = ''
            after_visible_no_space = _remove_visible_spaces(after_text)
            if before_visible_no_space and after_visible_no_space != before_visible_no_space:
                undo = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                try:
                    if callable(undo):
                        for _i in range(delete_back_count):
                            undo('Undo')
                except Exception:
                    pass
                raise LocalCliRuntimeError('paragraph_join_previous_exact changed visible text; undo attempted and mutation refused')

            after_target = self._find_text_on_expected_page(hwp, match=match, expected_page=min(expected_page, max_page_after))
            after_page = after_target.get('page')
            if after_page is None or int(after_page) > max_page_after:
                raise LocalCliRuntimeError(f'paragraph_join_previous_exact target page after mutation exceeds max_page_after={max_page_after}: {after_page}')
            _set_pos(hwp, int(after_target['pos'][0]), int(after_target['pos'][1]), 0)
            context_after = _capture_nearby_text_context(hwp)
            return {
                'schema_version': 'local-cli/paragraph-join-previous-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-join-previous',
                'match': match,
                'expected_page': expected_page,
                'delete_back_count': delete_back_count,
                'target_before': target,
                'target_after': after_target,
                'context_before': context_before,
                'context_after': context_after,
                'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
                'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
                'raw_results': [bool(item) if item is not None else None for item in raw_results],
                'warnings': ['Rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_paragraph_join_next_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_join_next_exact requires positive expected_page')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_join_next_exact requires confirm_layout=true')
        delete_count = int(step.get('delete_count') or 1)
        if not (1 <= delete_count <= 5):
            raise LocalCliRuntimeError('paragraph_join_next_exact delete_count must be 1..5')
        next_match = str(step.get('next_match') or '').strip() or None
        max_next_page_after = int(step.get('max_next_page_after') or expected_page)
        expected_next_contains = str(step.get('expected_next_contains') or '').strip() or None

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            before_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    before_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    before_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                before_text = ''
            before_visible_no_space = _remove_visible_spaces(before_text)

            target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            selected = _get_selected_pos(hwp)
            if not (selected and selected[0]):
                raise LocalCliRuntimeError('paragraph_join_next_exact expected active selection after find')
            _, _slist, _spara, _spos, elist, epara, epos = selected
            _set_pos(hwp, int(elist), int(epara), int(epos))
            moved_to_line_end = False
            if bool(step.get('move_to_line_end')):
                run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
                if not callable(run):
                    raise LocalCliRuntimeError('paragraph_join_next_exact requires HAction.Run')
                run('MoveLineEnd')
                moved_to_line_end = True
            context_before = _capture_nearby_text_context(hwp)
            if expected_next_contains:
                next_preview = str(context_before.get('next_paragraph_preview') or '')
                if expected_next_contains not in next_preview:
                    raise LocalCliRuntimeError(
                        f'paragraph_join_next_exact next paragraph guard failed: expected {expected_next_contains!r}; got {next_preview!r}'
                    )

            run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
            if not callable(run):
                raise LocalCliRuntimeError('paragraph_join_next_exact requires HAction.Run')
            raw_results = []
            for _i in range(delete_count):
                raw_results.append(run('Delete'))
            inserted_line_break = False
            if bool(step.get('insert_line_break')):
                raw_results.append(run('BreakLine'))
                inserted_line_break = True

            after_text = ''
            try:
                if hasattr(hwp, 'get_text_file'):
                    after_text = str(hwp.get_text_file('UNICODE', '') or '')
                elif hasattr(hwp, 'GetTextFile'):
                    after_text = str(hwp.GetTextFile('UNICODE', '') or '')
            except Exception:
                after_text = ''
            after_visible_no_space = _remove_visible_spaces(after_text)
            if before_visible_no_space and after_visible_no_space != before_visible_no_space:
                try:
                    for _i in range(delete_count):
                        run('Undo')
                except Exception:
                    pass
                diff_at = next((i for i, (a, b) in enumerate(zip(before_visible_no_space, after_visible_no_space)) if a != b), min(len(before_visible_no_space), len(after_visible_no_space)))
                before_frag = before_visible_no_space[max(0, diff_at-40):diff_at+80]
                after_frag = after_visible_no_space[max(0, diff_at-40):diff_at+80]
                raise LocalCliRuntimeError(
                    'paragraph_join_next_exact changed visible text; undo attempted and mutation refused; '
                    f'before_len={len(before_visible_no_space)} after_len={len(after_visible_no_space)} diff_at={diff_at} '
                    f'before_frag={before_frag!r} after_frag={after_frag!r}'
                )

            after_target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            after_next = None
            if next_match:
                # The page target may move upward after a successful join, so search the expected page first,
                # then permit a bounded target page through the same exact finder.
                try:
                    after_next = self._find_text_on_expected_page(hwp, match=next_match, expected_page=max_next_page_after)
                except LocalCliRuntimeError:
                    after_next = self._find_text_on_expected_page(hwp, match=next_match, expected_page=expected_page)
                next_page = after_next.get('page')
                if next_page is None or int(next_page) > max_next_page_after:
                    raise LocalCliRuntimeError(f'paragraph_join_next_exact next_match page after mutation exceeds max_next_page_after={max_next_page_after}: {next_page}')
            _set_pos(hwp, int(after_target['pos'][0]), int(after_target['pos'][1]), int(after_target['pos'][2]))
            context_after = _capture_nearby_text_context(hwp)
            return {
                'schema_version': 'local-cli/paragraph-join-next-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-join-next',
                'match': match,
                'expected_page': expected_page,
                'delete_count': delete_count,
                'inserted_line_break': inserted_line_break,
                'moved_to_line_end': moved_to_line_end,
                'target_before': target,
                'target_after': after_target,
                'next_after': after_next,
                'context_before': context_before,
                'context_after': context_after,
                'visible_text_no_space_hash_before': self._text_proof_hash(before_visible_no_space),
                'visible_text_no_space_hash_after': self._text_proof_hash(after_visible_no_space),
                'raw_results': [bool(item) if item is not None else None for item in raw_results],
                'warnings': ['Rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _bundle_paragraph_style_apply_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        match = self._bundle_require_text(step, 'match', max_chars=500)
        expected_page = int(step.get('expected_page') or 0)
        if expected_page <= 0:
            raise LocalCliRuntimeError('paragraph_style_apply_exact requires positive expected_page')
        if not bool(step.get('confirm_layout')):
            raise LocalCliRuntimeError('paragraph_style_apply_exact requires confirm_layout=true')
        desired: dict[str, Any] = {}
        if step.get('keep_with_next') is not None:
            desired['KeepWithNext'] = 1 if bool(step.get('keep_with_next')) else 0
        if step.get('widow_orphan') is not None:
            desired['WidowOrphan'] = 1 if bool(step.get('widow_orphan')) else 0
        if step.get('pagebreak_before') is not None:
            desired['PagebreakBefore'] = int(step.get('pagebreak_before'))
        if not desired:
            raise LocalCliRuntimeError('paragraph_style_apply_exact requires at least one style field')

        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        try:
            text_occurrence_count = None
            try:
                if hasattr(hwp, 'get_text_file'):
                    text_occurrence_count = str(hwp.get_text_file('UNICODE', '') or '').count(match)
                elif hasattr(hwp, 'GetTextFile'):
                    text_occurrence_count = str(hwp.GetTextFile('UNICODE', '') or '').count(match)
            except Exception:
                text_occurrence_count = None
            target = self._find_text_on_expected_page(hwp, match=match, expected_page=expected_page)
            target_snapshot = self._bundle_compact_snapshot(hwp)
            page = target.get('page')
            if page is None:
                raise LocalCliRuntimeError('paragraph_style_apply_exact cannot prove target page')
            before_style = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('KeepWithNext', 'WidowOrphan', 'PagebreakBefore', 'LineSpacing', 'LineSpacingType', 'PrevSpacing', 'NextSpacing'),
            )
            set_para = getattr(hwp, 'set_para', None)
            if not callable(set_para):
                raise LocalCliRuntimeError('paragraph_style_apply_exact requires pyhwpx set_para')
            raw = set_para(**desired)
            after_style = self._style_parameter_snapshot(
                hwp,
                'ParagraphShape',
                'HParaShape',
                ('KeepWithNext', 'WidowOrphan', 'PagebreakBefore', 'LineSpacing', 'LineSpacingType', 'PrevSpacing', 'NextSpacing'),
            )
            before_values = before_style.get('values') if isinstance(before_style.get('values'), dict) else {}
            after_values = after_style.get('values') if isinstance(after_style.get('values'), dict) else {}
            changed = {
                key: {'before': before_values.get(key), 'after': after_values.get(key)}
                for key in desired
                if before_values.get(key) != after_values.get(key)
            }
            satisfied = {key: after_values.get(key) for key in desired if after_values.get(key) == desired.get(key)}
            if len(satisfied) != len(desired):
                raise LocalCliRuntimeError(
                    f'paragraph_style_apply_exact did not observe desired style values; desired={desired!r}; before={before_values!r}; after={after_values!r}'
                )
            after_snapshot = self._bundle_compact_snapshot(hwp)
            return {
                'schema_version': 'local-cli/paragraph-style-apply-exact/v1',
                'read_only': False,
                'mutation': 'paragraph-style-apply',
                'match': match,
                'expected_page': expected_page,
                'text_occurrence_count': text_occurrence_count,
                'target': {'page': page, 'snapshot': target_snapshot, 'finder': target},
                'operation': {'desired': desired, 'raw_result': bool(raw) if raw is not None else None},
                'before_style': before_style,
                'after_style': after_style,
                'changed_style': changed,
                'after': after_snapshot,
                'warnings': ['This primitive mutates one unique matched paragraph style only; rendered before/after proof is required before accepting the working copy.'],
            }
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass

    def _execute_command_bundle_step(
        self,
        handle: LocalCliRuntimeHandle,
        step: dict[str, Any],
        *,
        binding: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool, list[str]]:
        op = str(step.get('op') or '')
        warnings: list[str] = []

        package = self.command_packages.get(op)
        if package is not None:
            return package.run(service=self, handle=handle, step=step, binding=binding)

        if op == 'control_inventory':
            result = self._bundle_control_inventory(handle.hwp, step)
            return result, False, list(result.get('warnings') or [])

        if op == 'table_frame_inventory':
            result = self._bundle_table_frame_inventory(handle.hwp, step)
            return result, False, list(result.get('warnings') or [])

        if op == 'control_delete_exact':
            result = self._bundle_control_delete_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'exact_control_select_proof':
            result = self._bundle_exact_control_select_proof(handle.hwp, step)
            return result, False, list(result.get('warnings') or [])

        if op == 'table_cell_structure_exact':
            result = self._bundle_table_cell_structure_exact(handle.hwp, step)
            return result, False, list(result.get('warnings') or [])

        if op == 'table_column_width_exact':
            result = self._bundle_table_column_width_exact(handle, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'paragraph_rehome_exact':
            result = self._bundle_paragraph_rehome_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'control_join_previous_exact':
            result = self._bundle_control_join_previous_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'control_move_resize_exact':
            result = self._bundle_control_move_resize_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'cell_row_fit_exact':
            result = self._bundle_cell_row_fit_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'table_split_exact':
            result = self._bundle_table_split_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'cell_format_exact':
            result = self._bundle_cell_format_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'style_inspect':
            result = self._bundle_style_inspect(handle.hwp, step)
            return result, False, list(result.get('warnings') or [])

        if op == 'paragraph_style_apply_exact':
            result = self._bundle_paragraph_style_apply_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'paragraph_delete_exact':
            result = self._bundle_paragraph_delete_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'paragraph_join_previous_exact':
            result = self._bundle_paragraph_join_previous_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'paragraph_join_next_exact':
            result = self._bundle_paragraph_join_next_exact(handle.hwp, step)
            return result, True, list(result.get('warnings') or [])

        if op == 'export_pdf':
            artifact_path = export_document_pdf(
                session_root=handle.session_root,
                source_filename=handle.source_filename,
                hwp=handle.hwp,
                log_path=handle.log_path,
            )
            return {
                'artifact_kind': 'export',
                'artifact_path': str(artifact_path),
                'filename': self._artifact_name(kind='export', source_filename=handle.source_filename),
                'download_path': self._artifact_download_path(session_id=handle.session_id, kind='export'),
            }, False, warnings

        if op == 'hwp_action':
            action = self._validate_action_name(str(step.get('action_name') or ''))
            if action not in _BUNDLE_SAFE_HACTION_NAMES:
                safe = ', '.join(sorted(_BUNDLE_SAFE_HACTION_NAMES))
                raise LocalCliRuntimeError(f'hwp_action {action!r} is not allowed in command-bundle. Safe actions: {safe}')
            run = getattr(getattr(handle.hwp, 'HAction', None), 'Run', None)
            if not callable(run):
                raise LocalCliRuntimeError('HAction.Run is unavailable on this machine')
            raw_result = run(action)
            succeeded = raw_result is None or bool(raw_result)
            warnings.append('hwp_action is allowlisted for navigation/selection only; destructive actions such as Delete/Erase are rejected.')
            return {
                'action': action,
                'succeeded': succeeded,
                'result_type': type(raw_result).__name__,
                'result_preview': self._macro_result_preview(raw_result),
                'snapshot': self._bundle_compact_snapshot(handle.hwp),
            }, False, warnings

        if op == 'pyhwpx_call':
            path, segments = self._validate_macro_path(str(step.get('method_path') or ''))
            if path not in _BUNDLE_SAFE_PYHWPX_CALLS:
                safe = ', '.join(sorted(_BUNDLE_SAFE_PYHWPX_CALLS))
                raise LocalCliRuntimeError(f'pyhwpx_call {path!r} is not allowed in command-bundle. Safe paths: {safe}')
            cleaned_args, cleaned_kwargs = self._validate_macro_args(step.get('args') or [], step.get('kwargs') or {})
            leaf = self._resolve_public_macro_leaf(handle.hwp, segments)
            mode = 'call' if callable(leaf) else 'property'
            if mode == 'property' and (cleaned_args or cleaned_kwargs):
                raise LocalCliRuntimeError('pyhwpx_call property access does not accept args or kwargs')
            raw_result = leaf(*cleaned_args, **cleaned_kwargs) if callable(leaf) else leaf
            if not (raw_result is None or isinstance(raw_result, (bool, int, float, str, list, tuple, dict))):
                raise LocalCliRuntimeError('pyhwpx_call result is not JSON-previewable')
            return {
                'path': path,
                'mode': mode,
                'result_type': type(raw_result).__name__,
                'result_preview': self._macro_result_preview(raw_result),
                'snapshot': self._bundle_compact_snapshot(handle.hwp),
            }, False, warnings

        if op == 'save_document':
            save_document(handle.hwp)
            return {
                'schema_version': 'local-cli/save-document/v1',
                'read_only': False,
                'mutation': 'save-document',
                'snapshot': self._bundle_compact_snapshot(handle.hwp),
            }, False, warnings

        if op == 'set_text_file':
            text = self._bundle_require_text(step, 'text')
            fmt = str(step.get('format') or 'UNICODE').strip().upper()
            option = str(step.get('option') or 'insertfile').strip().lower()
            if fmt != 'UNICODE' or option != 'insertfile':
                raise LocalCliRuntimeError('set_text_file command-bundle op only supports format=UNICODE and option=insertfile')
            before_snapshot = self._bundle_compact_snapshot(handle.hwp)
            strategy = self._insert_text_file_at_caret(handle.hwp, text=text, session_root=handle.session_root)
            after_snapshot = self._bundle_compact_snapshot(handle.hwp)
            raw_target_readback, readback_warnings = self._capture_set_text_file_target_readback(
                handle.hwp,
                session_root=handle.session_root,
                intended_text=text,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                insert_strategy=strategy,
                fail_on_mismatch=None,
            )
            warnings.extend(readback_warnings)
            return {
                'text_len': len(text),
                'text_hash': self._text_proof_hash(text),
                'strategy': strategy,
                'raw_target_readback': raw_target_readback,
                'snapshot': self._bundle_compact_snapshot(handle.hwp),
            }, True, warnings

        if op == 'anchor_insert':
            result = self._perform_anchor_insert(
                handle.hwp,
                target=str(step.get('target') or ''),
                position=str(step.get('position') or 'before-anchor'),
                text=self._anchor_insert_text_from_step(step),
                session_root=handle.session_root,
            )
            return result, True, list(result.get('warnings') or [])

        if op == 'get_selected_text':
            keep_select = step.get('keep_select') is not False
            result = self._capture_selected_text_proof_for_bundle(handle.hwp, keep_select=keep_select, selection_cache=binding)
            warnings.extend(str(item) for item in result.get('warnings') or [])
            return result, False, warnings

        if op == 'where':
            location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
            )
            return {'location': self._bundle_compact_location(location)}, False, warnings

        raise LocalCliRuntimeError(f'Unsupported command-bundle op: {op}')

    def _record_local_cli_command(
        self,
        command: str,
        *,
        binding: dict[str, Any],
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        session_id = str(binding.get('session_id') or '').strip()
        if not session_id:
            return
        try:
            semantic_ok = payload.get('semantic_ok') if isinstance(payload.get('semantic_ok'), bool) else (
                payload.get('ok') if isinstance(payload.get('ok'), bool) else None
            )
            self.interactive_sessions.record_command(
                command,
                session_id=session_id,
                state='failed' if semantic_ok is False else 'succeeded',
                summary=summary,
                payload=payload,
                metadata={'local_cli_v1': {'bridge': 'local_cli_v1'}},
            )
        except Exception:
            pass

    def _execute_live(
        self,
        *,
        binding: dict[str, Any],
        command_name: str,
        task_label: str,
        handler: Callable[[LocalCliRuntimeHandle], T],
        timeout: float = 90.0,
    ) -> T:
        self._require_ready_runtime(task_label)
        session_id = self._binding_session_id(binding)
        current_binding = self._read_binding(session_id=session_id)
        if not isinstance(current_binding, dict):
            raise LocalCliServiceError(
                'Live local CLI binding is unavailable. Re-open the document.',
                status_code=409,
            )
        binding.clear()
        binding.update(current_binding)
        binding['_binding_base'] = copy.deepcopy(current_binding)
        try:
            command_generation = int(binding.get('command_generation', 0))
        except (TypeError, ValueError) as exc:
            raise LocalCliServiceError(
                'Live local CLI binding generation is invalid.',
                status_code=500,
            ) from exc
        try:
            native_command_sequence = int(binding.get('native_command_sequence', 0))
        except (TypeError, ValueError) as exc:
            raise LocalCliServiceError(
                'Live local CLI native command sequence is invalid.',
                status_code=500,
            ) from exc
        if not self.runtime_manager.has_session(session_id):
            self._cleanup_stale_binding(binding)
            raise LocalCliServiceError('Live local CLI session is unavailable. Re-open the document.', status_code=409)
        try:
            result = self.runtime_manager.execute(
                session_id=session_id,
                command_name=command_name,
                handler=handler,
                timeout=timeout,
            )
            binding['_expected_command_generation'] = command_generation
            try:
                command_status = self.runtime_manager.command_status(session_id)
            except Exception:
                command_status = {}
            if isinstance(result, dict):
                result = dict(result)
                semantic_ok = command_status.get('semantic_ok')
                if isinstance(semantic_ok, bool):
                    # The handler's envelope may have been assembled before a
                    # later bundle step failed.  Normalize before redaction so
                    # the adapter cannot report a semantic failure as success.
                    result['ok'] = semantic_ok
                    result['semantic_ok'] = semantic_ok
                if isinstance(command_status.get('may_have_mutated'), bool):
                    result['may_have_mutated'] = command_status['may_have_mutated']
                if isinstance(command_status.get('failed_step_count'), int):
                    result['failed_step_count'] = command_status['failed_step_count']
                if isinstance(command_status.get('step_count'), int):
                    result['step_count'] = command_status['step_count']
                result['_local_cli_command'] = {
                    'command': command_name,
                    'generation': command_generation,
                    'command_id': command_status.get('command_id'),
                    'sequence': command_status.get('sequence', native_command_sequence),
                    'state': command_status.get('state', 'succeeded'),
                    'semantic_ok': command_status.get('semantic_ok'),
                    'recovery': command_status.get('recovery'),
                }
            command_sequence = command_status.get('sequence')
            if isinstance(command_sequence, int) and command_sequence >= native_command_sequence:
                binding['_expected_native_command_sequence'] = native_command_sequence
                binding['native_command_sequence'] = command_sequence
                binding.pop('pending_command', None)
            return result
        except LocalCliRuntimeError as exc:
            if isinstance(exc, LocalCliRuntimeTimeoutError):
                try:
                    command_status = self.runtime_manager.command_status(session_id, exc.command_id)
                except Exception:
                    command_status = {'command_id': exc.command_id, 'state': exc.command_state}
                command_sequence = command_status.get('sequence', native_command_sequence)
                try:
                    command_sequence = max(native_command_sequence, int(command_sequence))
                except (TypeError, ValueError):
                    command_sequence = native_command_sequence
                binding['_expected_command_generation'] = command_generation
                binding['_expected_native_command_sequence'] = native_command_sequence
                binding['native_command_sequence'] = command_sequence
                binding['pending_command'] = {
                    'command_id': exc.command_id,
                    'command': command_name,
                    'sequence': command_sequence,
                    'state': command_status.get('state', exc.command_state),
                    'timed_out_at': utc_now_iso(),
                }
                self._save_binding(binding)
                try:
                    self.interactive_sessions.record_command(
                        command_name,
                        session_id=session_id,
                        state='pending',
                        summary=f'{command_name} timed out; awaiting native reconciliation',
                        payload={'command_id': exc.command_id, 'sequence': command_sequence},
                        metadata={'local_cli_v1': {'reconciliation_pending': True}},
                        live_runtime={
                            'reconciliation_pending': True,
                            'pending_command': dict(binding['pending_command']),
                        },
                    )
                except Exception:
                    pass
                raise LocalCliServiceError(
                    f'{exc} status={command_status.get("state", exc.command_state)} '
                    f'command_id={exc.command_id}; run command-reconcile before retrying.',
                    status_code=504,
                ) from exc
            if self._looks_like_stale_live_session_error(exc):
                self._cleanup_stale_binding(
                    binding,
                    summary='Local CLI live session became stale after the Hancom bridge returned a COM error.',
                    outcome='stale',
                )
                raise LocalCliServiceError('Live local CLI session became stale. Re-open the document.', status_code=409) from exc
            if not self.runtime_manager.has_session(session_id):
                self._cleanup_stale_binding(binding)
                raise LocalCliServiceError('Live local CLI session is unavailable. Re-open the document.', status_code=409) from exc
            raise LocalCliServiceError('Local CLI native command failed.', status_code=500) from exc

    def _snapshot_temp_hwpx(self, handle: LocalCliRuntimeHandle, *, purpose: str) -> Path:
        snapshot_path = handle.session_root / 'metadata' / f'{purpose}-{uuid.uuid4().hex}.hwpx'
        try:
            save_hwp_as(handle.hwp, snapshot_path, 'HWPX', handle.log_path)
        except Exception as exc:
            raise LocalCliRuntimeError(f'Failed to snapshot the live document for {purpose}: {exc}') from exc
        if not snapshot_path.exists():
            raise LocalCliRuntimeError(f'Live document snapshot was not created for {purpose}.')
        return snapshot_path

    def _paths_match(self, left: str | Path | None, right: str | Path | None) -> bool:
        if left in (None, '') or right in (None, ''):
            return False
        left_norm = str(left).replace('\\', '/').rstrip('/').casefold()
        right_norm = str(right).replace('\\', '/').rstrip('/').casefold()
        if left_norm == right_norm:
            return True
        # The live COM snapshot usually reports an absolute Windows path while
        # the session handle can carry a path relative to the writer root
        # (`spool/.../working-copy.hwpx`). Treat that exact suffix relation as
        # the same working copy, but never as a fuzzy filename-only match.
        return left_norm.endswith(f'/{right_norm}') or right_norm.endswith(f'/{left_norm}')

    def _ensure_active_working_copy(self, handle: LocalCliRuntimeHandle, *, purpose: str) -> dict[str, Any]:
        location = snapshot_live_location(
            hwp=handle.hwp,
            source_filename=handle.source_filename,
            working_copy_id=handle.session_id,
            include_nearby_context=False,
            include_document_snapshot=True,
        )
        if self._paths_match(location.get('document_path'), handle.working_copy_path):
            return location

        open_method = getattr(handle.hwp, 'open', None)
        if not callable(open_method):
            open_method = getattr(handle.hwp, 'Open', None)
        if callable(open_method):
            open_method(str(handle.working_copy_path))
            location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
                include_nearby_context=False,
                include_document_snapshot=True,
            )
            if self._paths_match(location.get('document_path'), handle.working_copy_path):
                return location

        raise LocalCliRuntimeError(
            f'Active document is not the live working copy during {purpose}; refusing to continue.'
        )

    def _get_live_document_text(self, handle: LocalCliRuntimeHandle, *, purpose: str) -> str:
        self._ensure_active_working_copy(handle, purpose=f'{purpose}:before_text_extract')
        if hasattr(handle.hwp, 'get_text_file'):
            text = handle.hwp.get_text_file(format='UNICODE', option='')
        elif hasattr(handle.hwp, 'GetTextFile'):
            text = handle.hwp.GetTextFile('UNICODE', '')
        else:
            raise LocalCliRuntimeError('pyhwpx get_text_file/GetTextFile is unavailable on this machine')
        self._ensure_active_working_copy(handle, purpose=f'{purpose}:after_text_extract')
        return str(text or '')

    def _live_paragraph_records(self, handle: LocalCliRuntimeHandle, *, purpose: str) -> list[dict[str, Any]]:
        text = self._get_live_document_text(handle, purpose=purpose)
        paragraphs = load_plain_text_records(text)
        if paragraphs:
            return paragraphs
        try:
            return load_paragraph_records(self._working_copy_path({'working_copy_path': str(handle.working_copy_path)}))
        except LocalCliDocumentError as exc:
            raise LocalCliRuntimeError(str(exc)) from exc

    async def open_upload(self, *, file: UploadFile, session_label: str | None = None) -> dict[str, Any]:
        self._require_ready_runtime('local_cli.open')
        active_binding = self._read_binding()
        if isinstance(active_binding, dict):
            active_session_id = str(active_binding.get('session_id') or '').strip()
            if self._binding_has_pending_reconciliation(active_binding):
                pending = active_binding.get('pending_command') if isinstance(active_binding.get('pending_command'), dict) else {}
                raise LocalCliServiceError(
                    'A native local CLI command is unresolved; reconcile '
                    f"command_id={str(pending.get('command_id') or '').strip()} before opening another document.",
                    status_code=409,
                )
            if active_session_id and self.runtime_manager.has_session(active_session_id):
                raise LocalCliServiceError('A local CLI document is already open. Close it before opening another one.', status_code=409)
            if (
                active_binding.get('document_session_state') in {'reconciled', 'reconciled_cleanup_pending'}
                or isinstance(active_binding.get('artifact_custody'), dict)
            ):
                raise LocalCliServiceError(
                    'A reconciled local CLI session must be explicitly closed after downloading its artifacts.',
                    status_code=409,
                )
            if not self._cleanup_stale_binding(active_binding):
                raise LocalCliServiceError(
                    'The previous local CLI session is unavailable and its managed root is retained; '
                    'retry cleanup before opening another document.',
                    status_code=409,
                )

        filename = Path(file.filename or 'upload.hwpx').name
        suffix = Path(filename).suffix.lower() or '.hwpx'
        if suffix not in self.settings.allowed_extensions_list:
            raise LocalCliServiceError(
                f'Unsupported file type: {suffix or "<none>"}. Allowed: {self.settings.allowed_extensions_list}',
                status_code=400,
            )

        session_id = uuid.uuid4().hex
        session_root = self.sessions_root / session_id
        ensure_session_layout(session_root)
        upload_dir = session_root / 'upload'
        working_dir = session_root / 'working'
        uploaded_path = upload_dir / f'original{suffix}'
        working_copy_path = working_dir / f'working-copy{suffix}'
        size_bytes = 0

        try:
            with uploaded_path.open('wb') as target:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.settings.max_upload_mb * 1024 * 1024:
                        raise LocalCliServiceError('Upload exceeds configured size limit.', status_code=413)
                    target.write(chunk)
            if size_bytes <= 0:
                raise LocalCliServiceError('Empty upload is not allowed.', status_code=400)

            shutil.copy2(uploaded_path, working_copy_path)
            try:
                session = self.interactive_sessions.open_session(
                    source_path=working_copy_path,
                    source_filename=filename,
                    file_size_bytes=size_bytes,
                    content_type=file.content_type,
                    session_label=session_label,
                    metadata={'local_cli_v1': {'opened_via': 'local_cli_v1'}},
                    session_id=session_id,
                )
            except Exception as exc:
                raise LocalCliServiceError(str(exc), status_code=409) from exc

            resolved_session_id = str(session.get('session_id') or '').strip()
            if not resolved_session_id:
                raise LocalCliServiceError('Server did not return a valid local CLI session id.', status_code=500)
            if resolved_session_id != session_id:
                raise LocalCliServiceError(
                    'Local CLI session id mismatch between the API session record and the live runtime session.',
                    status_code=500,
                )

            try:
                runtime_open = self.runtime_manager.open_session(
                    session_id=session_id,
                    session_root=session_root,
                    working_copy_path=working_copy_path,
                    source_filename=filename,
                )
            except LocalCliRuntimeTimeoutError as exc:
                try:
                    command_status = self.runtime_manager.command_status(session_id, exc.command_id)
                except Exception:
                    command_status = {'command_id': exc.command_id, 'state': exc.command_state}
                try:
                    command_sequence = command_status.get('sequence', 1)
                    if isinstance(command_sequence, bool) or not isinstance(command_sequence, int) or command_sequence <= 0:
                        raise ValueError('invalid startup timeout sequence')
                except (TypeError, ValueError) as sequence_exc:
                    raise LocalCliServiceError('Local CLI startup reconciliation sequence is invalid.', status_code=500) from sequence_exc
                root_identity = self._managed_path_identity(session_root)
                if not isinstance(root_identity, dict):
                    raise LocalCliServiceError('Server-managed session root identity could not be captured.', status_code=500)
                binding = {
                    'session_id': session_id,
                    'session_root_path': str(session_root),
                    'session_root_identity': root_identity,
                    'source_filename': filename,
                    'uploaded_path': str(uploaded_path),
                    'working_copy_path': str(working_copy_path),
                    'opened_at': utc_now_iso(),
                    'updated_at': utc_now_iso(),
                    'command_generation': 0,
                    'native_command_sequence': command_sequence,
                    'document_session_state': 'timed_out_pending_reconciliation',
                    'live_session_bound': True,
                    'working_copy_dirty': False,
                    'pending_command': {
                        'command_id': exc.command_id,
                        'command': 'start',
                        'sequence': command_sequence,
                        'state': command_status.get('state', exc.command_state),
                        'timed_out_at': utc_now_iso(),
                    },
                    'artifacts': {'latest_working_copy_path': str(working_copy_path)},
                }
                working_copy_custody = {}
                self._verify_artifact_readback(binding, working_copy_path, readback=working_copy_custody)
                binding['artifact_custody'] = {'working-copy': working_copy_custody}
                self._save_binding(binding)
                try:
                    self.interactive_sessions.record_command(
                        'open',
                        session_id=session_id,
                        state='pending',
                        summary='open timed out; awaiting native reconciliation',
                        payload={'command_id': exc.command_id, 'sequence': command_sequence},
                        metadata={'local_cli_v1': {'reconciliation_pending': True}},
                        live_runtime={
                            'reconciliation_pending': True,
                            'pending_command': dict(binding['pending_command']),
                        },
                    )
                except Exception:
                    pass
                raise LocalCliServiceError(
                    f'{exc} status={command_status.get("state", exc.command_state)} '
                    f'command_id={exc.command_id}; run command-reconcile before retrying.',
                    status_code=504,
                ) from exc
            location = runtime_open.get('location') if isinstance(runtime_open.get('location'), dict) else {}

            binding = {
                'session_id': session_id,
                'session_root_path': str(session_root),
                'session_root_identity': self._managed_path_identity(session_root),
                'source_filename': filename,
                'uploaded_path': str(uploaded_path),
                'working_copy_path': str(working_copy_path),
                'opened_at': utc_now_iso(),
                'updated_at': utc_now_iso(),
                'command_generation': 0,
                'native_command_sequence': 0,
                'document_session_state': 'open',
                'live_session_bound': True,
                'working_copy_dirty': False,
                'last_find': None,
                'cursor_pos': None,
                'selected_range': None,
                'current_cell_addr': None,
                'last_cursor_snapshot': None,
                'last_live_location': None,
                'artifacts': {'latest_working_copy_path': str(working_copy_path)},
            }
            if not isinstance(binding['session_root_identity'], dict):
                raise LocalCliServiceError('Server-managed session root identity could not be captured.', status_code=500)
            working_copy_custody = {}
            self._verify_artifact_readback(binding, working_copy_path, readback=working_copy_custody)
            binding['artifact_custody'] = {'working-copy': working_copy_custody}
            binding = self._update_live_binding(binding, location=location, dirty=False)
            return {
                'ok': True,
                'session_id': session_id,
                'source_filename': filename,
                'working_copy_id': session_id,
                'cursor_summary': location.get('cursor_summary'),
            }
        except Exception:
            if session_id:
                runtime_close_ok = False
                try:
                    self.runtime_manager.close_session(session_id)
                    runtime_close_ok = True
                except Exception:
                    # The native runtime may still own the working copy.  Do
                    # not delete or clear its binding until teardown is known
                    # to have completed.
                    runtime_close_ok = False
                if runtime_close_ok:
                    cleanup_binding = {
                        'session_id': session_id,
                        'session_root_path': str(session_root),
                        'session_root_identity': self._managed_path_identity(session_root),
                    }
                    cleanup_ok = False
                    try:
                        self._cleanup_managed_session_root(cleanup_binding)
                        cleanup_ok = True
                    except Exception:
                        # Do not silently claim cleanup.  The root remains
                        # discoverable for an operator/reaper when identity-bound
                        # removal cannot be proven.
                        cleanup_ok = False
                    if cleanup_ok:
                        self._record_session_close(
                            session_id=session_id,
                            summary='Local CLI session failed during open.',
                            outcome='open_failed',
                            state='failed',
                        )
                        self._clear_binding(session_id=session_id)
            raise
        finally:
            await file.close()

    def reconcile_command(self, *, command_id: str, session_id: str | None = None) -> dict[str, Any]:
        """Reconcile one timed-out native command and commit its late result."""

        command_id = str(command_id or '').strip()
        if not command_id:
            raise LocalCliServiceError('command_id must not be empty.', status_code=400)
        binding = self._load_active_binding(session_id=session_id, require_live=False)
        pending = binding.get('pending_command') if isinstance(binding.get('pending_command'), dict) else {}
        pending_id = str(pending.get('command_id') or '').strip()
        if pending_id and pending_id != command_id:
            raise LocalCliServiceError(
                f'Local CLI binding is waiting for a different command: {pending_id}.',
                status_code=409,
            )
        status = self._command_status_for_binding(binding, command_id)
        if str(status.get('command_id') or '').strip() not in {'', command_id}:
            raise LocalCliServiceError('Native command identity did not match the requested reconciliation.', status_code=409)
        state = str(status.get('state') or 'unknown')
        if state == 'timed_out_pending_reconciliation':
            return {
                'ok': False,
                'reconciled': False,
                'reconciliation': 'pending',
                'session_id': self._binding_session_id(binding),
                'command': status,
            }
        if state not in {'completed_after_timeout', 'failed_after_timeout'}:
            raise LocalCliServiceError(
                f'Local CLI command is not awaiting reconciliation: state={state}.',
                status_code=409,
            )
        custody_reader = getattr(self.runtime_manager, 'command_custody', None)
        legacy_runtime_double = not callable(custody_reader)
        if not legacy_runtime_double and not status.get('reconciled'):
            try:
                status = self.runtime_manager.reconcile_command(
                    self._binding_session_id(binding),
                    command_id,
                    session_root=self._binding_session_root(binding),
                )
            except Exception as exc:
                raise LocalCliServiceError(
                    'Native command reconciliation could not complete; outcome remains unknown.',
                    status_code=409,
                ) from exc
            state = str(status.get('state') or 'unknown')
            if state == 'timed_out_pending_reconciliation':
                return {
                    'ok': False,
                    'reconciled': False,
                    'reconciliation': 'pending',
                    'session_id': self._binding_session_id(binding),
                    'command': status,
                }
            if state not in {'completed_after_timeout', 'failed_after_timeout'}:
                raise LocalCliServiceError(
                    f'Local CLI command is not awaiting reconciliation: state={state}.',
                    status_code=409,
                )
        if legacy_runtime_double:
            # Existing unit seams predate the custody API.  Keep this branch
            # limited to objects that cannot be the production runtime manager;
            # the real manager always exposes command_custody below.
            custody = status
        else:
            try:
                custody = custody_reader(
                    self._binding_session_id(binding),
                    command_id,
                    session_root=self._binding_session_root(binding),
                )
            except Exception as exc:
                raise LocalCliServiceError(
                    'Late command custody is unavailable; the native outcome is unknown and must not be promoted.',
                    status_code=409,
                ) from exc
        reconciliation_data = custody.get('reconciliation_data') if isinstance(custody.get('reconciliation_data'), dict) else {}
        if not legacy_runtime_double:
            private_version = reconciliation_data.get('version')
            private_session_id = str(reconciliation_data.get('session_id') or '').strip()
            private_command_id = str(reconciliation_data.get('command_id') or '').strip()
            private_sequence = reconciliation_data.get('sequence')
            if private_version != 1 or private_session_id != self._binding_session_id(binding) or private_command_id != command_id:
                raise LocalCliServiceError('Late command custody identity did not match the managed binding.', status_code=409)
            if isinstance(private_sequence, bool) or not isinstance(private_sequence, int) or private_sequence <= 0:
                raise LocalCliServiceError('Late command custody sequence is invalid.', status_code=409)
            if not isinstance(status.get('sequence'), int) or isinstance(status.get('sequence'), bool) or status['sequence'] != private_sequence:
                raise LocalCliServiceError('Late command custody sequence did not match the command status.', status_code=409)
            for field in ('semantic_ok', 'delta_dirty', 'document_modified_before_recovery'):
                value = reconciliation_data.get(field)
                if value is not None and not isinstance(value, bool):
                    raise LocalCliServiceError(f'Late command custody field is invalid: {field}.', status_code=409)
            if not isinstance(reconciliation_data.get('may_have_mutated'), bool):
                raise LocalCliServiceError('Late command custody mutation flag is invalid.', status_code=409)
            for field in ('step_count', 'failed_step_count'):
                value = reconciliation_data.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise LocalCliServiceError(f'Late command custody count is invalid: {field}.', status_code=409)
            if reconciliation_data['failed_step_count'] > reconciliation_data['step_count']:
                raise LocalCliServiceError('Late command custody step counts are inconsistent.', status_code=409)
        recovery = reconciliation_data.get('recovery') if isinstance(reconciliation_data.get('recovery'), dict) else {}
        public_recovery = status.get('recovery') if isinstance(status.get('recovery'), dict) else {}
        recovery_state = str(recovery.get('state') or public_recovery.get('state') or 'none')
        if recovery_state not in _RECOVERY_STATES:
            raise LocalCliServiceError('Late command recovery state is invalid.', status_code=409)
        if not legacy_runtime_double and recovery_state == 'preserved' and not str(recovery.get('attempt_id') or '').strip():
            raise LocalCliServiceError('Late command recovery attempt identity is missing.', status_code=409)
        if recovery_state == 'saving':
            return {
                'ok': False,
                'reconciled': False,
                'reconciliation': 'pending',
                'session_id': self._binding_session_id(binding),
                'command': status,
            }
        if not legacy_runtime_double and recovery_state != 'preserved':
            raise LocalCliServiceError(
                'Late command result has no committed native recovery artifact; outcome remains unknown.',
                status_code=409,
            )
        associated_command_id = str(binding.get('last_reconciliation_command_id') or '').strip()
        if not pending_id and not status.get('reconciled') and associated_command_id != command_id:
            raise LocalCliServiceError(
                'Local CLI command is not associated with a pending binding reconciliation.',
                status_code=409,
            )
        if status.get('reconciled') and not pending_id:
            custody_map = binding.get('artifact_custody') if isinstance(binding.get('artifact_custody'), dict) else {}
            committed_artifacts = binding.get('artifacts') if isinstance(binding.get('artifacts'), dict) else {}
            if not legacy_runtime_double and 'recovery' not in custody_map:
                raise LocalCliServiceError('Committed reconciliation has no recovery custody.', status_code=409)
            for kind, expected in custody_map.items():
                if not isinstance(expected, dict):
                    raise LocalCliServiceError('Committed artifact custody is malformed.', status_code=409)
                artifact_key = 'latest_working_copy_path' if kind in {'working-copy', 'working_copy'} else f'latest_{kind}_path'
                artifact_path = committed_artifacts.get(artifact_key)
                if not isinstance(artifact_path, str):
                    raise LocalCliServiceError('Committed artifact projection is missing.', status_code=409)
                try:
                    self._verify_artifact_readback(binding, Path(artifact_path), expected=expected)
                except (OSError, ValueError) as exc:
                    raise LocalCliServiceError('Committed artifact failed retry readback.', status_code=409) from exc
            stored_semantic_ok = (
                custody.get('semantic_ok') if isinstance(custody.get('semantic_ok'), bool) else
                status.get('semantic_ok') if isinstance(status.get('semantic_ok'), bool) else None
            )
            if not isinstance(stored_semantic_ok, bool):
                raise LocalCliServiceError('Stored semantic command outcome is unavailable; outcome remains unknown.', status_code=409)
            private_sequence = reconciliation_data.get('sequence')
            if (
                isinstance(private_sequence, bool)
                or not isinstance(private_sequence, int)
                or binding.get('native_command_sequence') != private_sequence
            ):
                raise LocalCliServiceError('Committed reconciliation sequence did not match the binding.', status_code=409)
            for expected in custody_map.values():
                if (
                    expected.get('command_id') not in (None, '', command_id)
                    or expected.get('sequence') not in (None, private_sequence)
                ):
                    raise LocalCliServiceError('Committed artifact custody identity did not match the command.', status_code=409)
            recovery_claim = recovery.get('artifact') if isinstance(recovery.get('artifact'), dict) else None
            recovery_entry = custody_map.get('recovery')
            if recovery_claim is None or recovery_entry is None or any(
                recovery_claim.get(key) != recovery_entry.get(key)
                for key in ('relative_path', 'sha256', 'size_bytes')
            ):
                raise LocalCliServiceError('Committed recovery claim did not match artifact custody.', status_code=409)
            return {
                'ok': stored_semantic_ok,
                'reconciled': True,
                'reconciliation': 'already_reconciled',
                'session_id': self._binding_session_id(binding),
                'command': status,
            }

        session_id = self._binding_session_id(binding)
        try:
            current_generation = int(binding.get('command_generation', 0))
            current_sequence = int(binding.get('native_command_sequence', 0))
            status_sequence = status.get('sequence')
            if isinstance(status_sequence, bool) or not isinstance(status_sequence, int) or status_sequence <= 0:
                raise ValueError('invalid native command sequence')
            command_sequence = status_sequence
        except (TypeError, ValueError) as exc:
            raise LocalCliServiceError('Local CLI reconciliation sequence is invalid.', status_code=500) from exc
        pending_sequence = pending.get('sequence')
        if pending_id and (
            isinstance(pending_sequence, bool)
            or not isinstance(pending_sequence, int)
            or pending_sequence != command_sequence
        ):
            raise LocalCliServiceError('Local CLI reconciliation sequence did not match its pending command.', status_code=409)
        if not pending_id and current_sequence != command_sequence:
            raise LocalCliServiceError('Local CLI reconciliation sequence did not match the binding.', status_code=409)
        result = status.get('result') if isinstance(status.get('result'), dict) else {}
        command_name = str(status.get('command') or pending.get('command') or '')
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        if not location and isinstance(result.get('after_location'), dict):
            location = result['after_location']
        artifacts = result.get('artifacts') if isinstance(result.get('artifacts'), dict) else {}
        root = self._binding_session_root(binding).resolve(strict=True)
        private_identity = reconciliation_data.get('session_root_identity')
        if not legacy_runtime_double:
            try:
                actual_stat = os.stat(root, follow_symlinks=False)
                actual_identity = {
                    'device': int(actual_stat.st_dev),
                    'inode': int(actual_stat.st_ino),
                    'mode': int(actual_stat.st_mode),
                }
            except OSError as exc:
                raise LocalCliServiceError('Recovery root custody could not be verified.', status_code=409) from exc
            if private_identity != actual_identity:
                raise LocalCliServiceError('Recovery root identity changed; native outcome remains unknown.', status_code=409)
        private_artifacts = reconciliation_data.get('artifacts') if isinstance(reconciliation_data.get('artifacts'), list) else []
        artifact_custody: dict[str, dict[str, Any]] = {}
        for entry in private_artifacts:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get('kind') or '')
            relative = str(entry.get('relative_path') or '')
            if not relative or kind not in {'recovery', 'export', 'screenshot', 'working-copy', 'working_copy'}:
                continue
            try:
                if (
                    relative.startswith(('/', '\\'))
                    or re.match(r'^[A-Za-z]:', relative)
                    or ':' in relative
                    or any(part in {'', '.', '..'} for part in relative.replace('\\', '/').split('/'))
                ):
                    raise ValueError('invalid managed artifact relative path')
                lexical = root / relative
                lexical.relative_to(root)
                current_path = root
                for part in lexical.relative_to(root).parts:
                    current_path = current_path / part
                    if current_path.is_symlink():
                        raise ValueError('symlinked managed artifact path')
                candidate = lexical.resolve(strict=True)
                candidate.relative_to(root)
                if candidate != lexical:
                    raise ValueError('managed artifact path resolves through a link')
                if not candidate.is_file() or candidate.is_symlink():
                    raise ValueError('not a regular managed artifact')
                expected_size = entry.get('size_bytes')
                expected_sha256 = entry.get('sha256')
                if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
                    raise ValueError('invalid committed artifact size')
                if not isinstance(expected_sha256, str) or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None:
                    raise ValueError('invalid committed artifact hash')
                digest = hashlib.sha256()
                actual_size = 0
                with candidate.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        actual_size += len(chunk)
                        digest.update(chunk)
                if actual_size != expected_size or digest.hexdigest() != expected_sha256:
                    raise ValueError('committed artifact changed after custody')
                artifact_custody[kind] = {
                    'relative_path': relative,
                    'sha256': expected_sha256,
                    'size_bytes': expected_size,
                    'command_id': command_id,
                    'sequence': command_sequence,
                }
                if kind == 'recovery':
                    artifacts = {**artifacts, 'latest_recovery_path': str(candidate)}
                    artifacts = {**artifacts, 'latest_recovery_sha256': expected_sha256, 'latest_recovery_size_bytes': expected_size}
                elif kind == 'export':
                    artifacts = {**artifacts, 'latest_export_path': str(candidate)}
                elif kind == 'screenshot':
                    artifacts = {**artifacts, 'latest_screenshot_path': str(candidate)}
                elif kind in {'working-copy', 'working_copy'}:
                    artifacts = {**artifacts, 'latest_working_copy_path': str(candidate)}
            except (OSError, ValueError) as exc:
                raise LocalCliServiceError('Committed recovery artifact failed managed-root readback.', status_code=409) from exc
        if not legacy_runtime_double:
            recovery_entry = artifact_custody.get('recovery')
            recovery_claim = recovery.get('artifact') if isinstance(recovery.get('artifact'), dict) else None
            if recovery_entry is None or recovery_claim is None or any(
                recovery_claim.get(key) != recovery_entry.get(key)
                for key in ('relative_path', 'sha256', 'size_bytes')
            ):
                raise LocalCliServiceError('Committed recovery custody has no matching recovery artifact.', status_code=409)
        semantic_ok = reconciliation_data.get('semantic_ok') if isinstance(reconciliation_data.get('semantic_ok'), bool) else (
            status.get('semantic_ok') if isinstance(status.get('semantic_ok'), bool) else None
        )
        if not isinstance(semantic_ok, bool):
            if legacy_runtime_double:
                semantic_ok = state == 'completed_after_timeout'
            else:
                raise LocalCliServiceError('Stored semantic command outcome is unavailable; outcome remains unknown.', status_code=409)
        delta_dirty = reconciliation_data.get('delta_dirty') if isinstance(reconciliation_data.get('delta_dirty'), bool) else (
            result.get('dirty') if legacy_runtime_double and isinstance(result.get('dirty'), bool) else None
        )
        may_have_mutated = reconciliation_data.get('may_have_mutated') is True or (
            legacy_runtime_double and result.get('dirty') is True
        )
        dirty, dirty_source = self._reduce_working_copy_dirty(
            prior_dirty=binding.get('working_copy_dirty') is True or binding.get('dirty') is True,
            semantic_ok=semantic_ok,
            delta_dirty=delta_dirty,
            may_have_mutated=may_have_mutated,
            command_name=command_name,
            fresh_document_modified=location.get('document_is_modified') if isinstance(location.get('document_is_modified'), bool) else None,
            fresh_sequence_matches=int(reconciliation_data.get('sequence', -1)) == int(status.get('sequence', -2)),
            ordinary_save_confirmed=reconciliation_data.get('ordinary_save_confirmed') is True,
        )
        if command_name == 'save':
            artifacts = {**artifacts, 'latest_working_copy_path': str(self._working_copy_path(binding))}
        if command_name == 'export':
            for entry in private_artifacts:
                if isinstance(entry, dict) and entry.get('kind') == 'export':
                    candidate = root / str(entry.get('relative_path') or '')
                    artifacts = {**artifacts, 'latest_export_path': str(candidate)}
                    break
        if state == 'failed_after_timeout':
            failure = status.get('error') if isinstance(status.get('error'), dict) else {}
            binding['last_reconciliation_error'] = {
                'command_id': command_id,
                'command': command_name,
                'error': failure,
                'recorded_at': utc_now_iso(),
            }
        binding['_expected_command_generation'] = current_generation
        binding['_expected_native_command_sequence'] = current_sequence
        binding['native_command_sequence'] = command_sequence
        binding['last_reconciliation_command_id'] = command_id
        binding['reconciliation_state'] = state
        if artifact_custody:
            binding['artifact_custody'] = artifact_custody
        if not location:
            location = binding.get('last_live_location') if isinstance(binding.get('last_live_location'), dict) else {}
        # First persist the artifact/location/dirty projection while the
        # binding still owns the pending command and the live session.
        binding = self._update_live_binding(binding, location=location, artifacts=artifacts, dirty=dirty)
        try:
            if legacy_runtime_double:
                acknowledged = self.runtime_manager.reconcile_command(
                    session_id,
                    command_id,
                    session_root=self._binding_session_root(binding),
                )
            else:
                acknowledged = self.runtime_manager.acknowledge_reconciliation(
                    session_id,
                    command_id,
                    session_root=self._binding_session_root(binding),
                )
        except Exception as exc:
            raise LocalCliServiceError(
                'Late command result could not be durably acknowledged; binding ownership remains pending.',
                status_code=500,
            ) from exc
        if not acknowledged.get('reconciled'):
            raise LocalCliServiceError(
                'Late command result was not durably marked reconciled; binding ownership remains pending.',
                status_code=500,
            )
        binding.pop('pending_command', None)
        binding['live_session_bound'] = False
        binding['document_session_state'] = 'reconciled'
        binding = self._save_binding(binding)
        cleanup_pending = False
        try:
            self.runtime_manager.close_session(session_id, timeout=30.0)
        except Exception:
            # Custody is already committed; leave the registered runtime for a
            # later explicit cleanup retry rather than reopening the document.
            cleanup_pending = True
            binding['cleanup_pending'] = True
            binding['document_session_state'] = 'reconciled_cleanup_pending'
            binding['live_session_bound'] = False
            try:
                binding = self._save_binding(binding)
            except Exception:
                pass
        try:
            self.interactive_sessions.record_command(
                command_name or 'native-command',
                session_id=session_id,
                state='succeeded' if semantic_ok is True else 'failed',
                summary=f'{command_name or "native command"} reconciled after timeout ({state})',
                payload={
                    'command_id': command_id,
                    'sequence': command_sequence,
                    'result': result,
                    'semantic_ok': semantic_ok,
                    'dirty': dirty,
                    'dirty_source': dirty_source,
                },
                metadata={'local_cli_v1': {'reconciliation': state, 'reconciliation_pending': False}},
                live_runtime={
                    'reconciliation_pending': False,
                    'pending_command': {'command_id': command_id, 'reconciled': True},
                },
            )
        except Exception:
            pass
        public_artifacts = self._public_artifacts(
            session_id=session_id,
            artifacts=binding.get('artifacts') if isinstance(binding.get('artifacts'), dict) else {},
            binding=binding,
        )
        recovery_download_path = public_artifacts.get('latest_recovery_download_path')
        return {
            'ok': bool(semantic_ok) if isinstance(semantic_ok, bool) else state == 'completed_after_timeout',
            'reconciled': True,
            'reconciliation': 'completed_after_timeout' if state == 'completed_after_timeout' else 'failed_after_timeout',
            'session_id': session_id,
            'command': acknowledged,
            'binding_generation': binding.get('command_generation'),
            'working_copy_dirty': binding.get('working_copy_dirty'),
            'artifacts': public_artifacts,
            'recovery_artifact_path': recovery_download_path,
            'recovery_artifact': {
                'download_path': recovery_download_path,
                **{
                    key: recovery['artifact'].get(key)
                    for key in ('sha256', 'size_bytes')
                    if recovery['artifact'].get(key) not in (None, '')
                },
            } if isinstance(recovery.get('artifact'), dict) and recovery_download_path else None,
            'cleanup_pending': cleanup_pending,
            'semantic_ok': semantic_ok,
            'dirty_source': dirty_source,
        }

    def status(self) -> dict[str, Any]:
        snapshot = self._runtime_snapshot()
        active_binding = self._read_binding()
        errors = snapshot.get('errors') if isinstance(snapshot, dict) and isinstance(snapshot.get('errors'), list) else []
        checks = snapshot.get('checks') if isinstance(snapshot, dict) and isinstance(snapshot.get('checks'), dict) else {}
        hancom_check = checks.get('hancom_automation') if isinstance(checks.get('hancom_automation'), dict) else {}
        ready = bool(snapshot and snapshot.get('ready'))
        blocked_reason = str(errors[0]).strip() if errors else None
        session_id = str((active_binding or {}).get('session_id') or '').strip() or None
        pending_reconciliation = (
            self._command_status_for_binding(active_binding)
            if isinstance(active_binding, dict) and self._binding_has_pending_reconciliation(active_binding)
            else None
        )
        cleanup_pending = bool(
            isinstance(active_binding, dict)
            and active_binding.get('document_session_state') in {'reconciled', 'reconciled_cleanup_pending'}
        )
        live_bound = bool(
            session_id
            and (not isinstance(active_binding, dict) or active_binding.get('live_session_bound') is not False)
            and self.runtime_manager.has_session(session_id)
        )
        if isinstance(active_binding, dict) and session_id:
            if pending_reconciliation is not None:
                live_bound = False
            elif live_bound:
                live_bound = self._probe_live_binding(active_binding)
            if not live_bound:
                active_binding = self._read_binding()
                if isinstance(active_binding, dict):
                    if self._binding_has_pending_reconciliation(active_binding):
                        pending_reconciliation = self._command_status_for_binding(active_binding)
                    if pending_reconciliation is not None:
                        pass
                    elif active_binding.get('document_session_state') in {
                        'reconciled', 'reconciled_cleanup_pending', 'closed_cleanup_pending'
                    }:
                        cleanup_pending = True
                    elif self._is_session_closed(session_id):
                        if active_binding.get('session_root_path'):
                            # A previous close may have released COM but failed
                            # managed-root deletion. Keep the ownership binding
                            # visible and make retrying cleanup the next action.
                            cleanup_pending = True
                            active_binding['live_session_bound'] = False
                            active_binding['document_session_state'] = 'closed_cleanup_pending'
                        else:
                            # Legacy bindings predate server-managed root
                            # custody, so there is no removable path left to
                            # prove before clearing their closed projection.
                            self._clear_binding(session_id=session_id, force=True)
                            active_binding = None
                    else:
                        active_binding['live_session_bound'] = False
                        active_binding['document_session_state'] = 'stale'
                        active_binding['updated_at'] = utc_now_iso()
                        self._save_binding(active_binding)

        artifacts = (active_binding or {}).get('artifacts') if isinstance(active_binding, dict) else None
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        public_artifacts = (
            self._public_artifacts(session_id=session_id, artifacts=artifacts, binding=active_binding)
            if session_id
            else {}
        )
        return {
            'ok': True,
            'runtime_up': ready,
            'hancom_attached': bool(hancom_check.get('ok')),
            'api_ready': True,
            'blocked_reason': blocked_reason,
            'next_action': (
                f'reconcile command {pending_reconciliation.get("command_id")}'
                if pending_reconciliation is not None
                else
                f'retry close cleanup for session {session_id}'
                if cleanup_pending
                else
                'open a file'
                if ready and not live_bound
                else 'continue with find/where/select or capture rendered proof before saving/reporting'
                if ready and live_bound
                else 'restore runtime readiness on the Windows Hancom worker'
            ),
            'session_id': session_id,
            'active_document': (active_binding or {}).get('source_filename'),
            'artifacts': public_artifacts,
            'last_proof_artifact': (
                public_artifacts.get('latest_screenshot_download_path')
                or public_artifacts.get('latest_export_download_path')
            ),
            'live_session_bound': live_bound,
            'command_reconciliation': pending_reconciliation,
            'working_copy_dirty': bool((active_binding or {}).get('working_copy_dirty')),
            'command_bundle_route_active': True,
            'server_primitive_version': 'local-cli-command-bundle/v2-style-inspect',
        }

    def find(
        self,
        *,
        query: str,
        session_id: str | None = None,
        around: int = 0,
        with_page: bool = False,
        proof_match: int | None = None,
    ) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            paragraphs = self._live_paragraph_records(handle, purpose='find')
            live_text = '\n'.join(str(item.get('text') or '') for item in paragraphs)
            document_text_hash = 'sha256:' + hashlib.sha256(live_text.encode('utf-8')).hexdigest()
            document_generation = f'local-cli/live-document/v1:{handle.session_id}:{document_text_hash}'
            try:
                matches = find_matches(
                    paragraphs,
                    query,
                    around=around,
                    with_page=with_page or proof_match is not None,
                )
            except LocalCliDocumentError as exc:
                raise LocalCliRuntimeError(str(exc)) from exc
            # Reject an invalid proof index before any live-location snapshot.  This
            # keeps the read-only 404 deterministic even on runtimes without get_pos.
            if proof_match is not None and proof_match > len(matches):
                raise LocalCliServiceError(f'No find match number {proof_match} for proof-match.', status_code=404)
            matches, enrich_warnings = self._enrich_live_find_matches_with_cursor_context(
                handle.hwp,
                query=query,
                matches=matches,
            )
            proof_payload: dict[str, Any] | None = None
            if proof_match is not None:
                proof_payload = dict(matches[proof_match - 1])
                # `proof_match` identifies a static paragraph, not the global
                # occurrence of that paragraph's full text. Keep searching for
                # the user's query and bind the live result to the selected
                # paragraph's identity/position instead.
                proof_query = query
                target_identity = dict(proof_payload.get('identity') or {})
                target_identity['normalized_hash'] = proof_payload.get('normalized_hash')
                target_identity['paragraph_normalized_hash'] = proof_payload.get('normalized_hash')
                target_identity['static_text'] = proof_payload.get('text')
                live_cursor_proof = proof_payload.get('live_cursor_proof')
                if isinstance(live_cursor_proof, Mapping):
                    live_pos = live_cursor_proof.get('pos')
                    if isinstance(live_pos, (list, tuple)) and len(live_pos) >= 2:
                        target_identity['live_position'] = [live_pos[0], live_pos[1]]
                original_snapshot: Mapping[str, Any] = {}
                try:
                    raw_snapshot = _snapshot_cursor_context(handle.hwp)
                    if isinstance(raw_snapshot, Mapping):
                        original_snapshot = raw_snapshot
                    live_match = self._find_live_match(
                        handle.hwp,
                        query=proof_query,
                        occurrence=1,
                        target_identity=target_identity,
                    )
                    live_snapshot = live_match.get('snapshot') if isinstance(live_match.get('snapshot'), Mapping) else {}
                    current_page = getattr(handle.hwp, 'current_page', None)
                    current_page = current_page() if callable(current_page) else current_page
                    try:
                        page = int(current_page)
                    except (TypeError, ValueError):
                        page = None
                    if page is not None and page > 0:
                        proof_payload['page'] = page
                        proof_payload['page_evidence'] = {
                            'method': 'current_page',
                            'value': page,
                            'authoritative': True,
                        }
                    proof_payload['live_cursor_proof'] = {
                        'occurrence': live_match.get('occurrence', 1),
                        'requested_match': proof_match,
                        'matched_query': live_match.get('matched_query'),
                        'match_strategy': live_match.get('match_strategy'),
                        'pos': live_snapshot.get('pos'),
                        'selected_pos': live_snapshot.get('selected_pos'),
                        'selected_text_preview': _preview_text(live_match.get('selected_text'), limit=120),
                        'target_identity': target_identity,
                        'document_generation': document_generation,
                    }
                    proof_payload['proof_generation'] = document_generation
                    proof_payload['session_id'] = handle.session_id
                    matches[proof_match - 1] = proof_payload
                finally:
                    original_pos = original_snapshot.get('pos')
                    if isinstance(original_pos, (list, tuple)) and len(original_pos) >= 3:
                        try:
                            _set_pos(handle.hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                        except Exception as exc:
                            enrich_warnings.append(
                                f'live find proof could not restore original caret position: {type(exc).__name__}: {exc}'
                            )
            return {
                'matches': matches,
                'warnings': enrich_warnings,
                'proof_match': proof_payload,
                'document_generation': document_generation,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='find', task_label='local_cli.find', handler=_handler)
        matches = result.get('matches') if isinstance(result.get('matches'), list) else []
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding['last_find'] = {
            'query': query,
            'matches': matches,
            'around': around,
            'with_page': with_page,
            'document_generation': result.get('document_generation'),
            'session_id': binding.get('session_id'),
            'updated_at': utc_now_iso(),
        }
        binding = self._update_live_binding(binding, location=location)
        self._record_local_cli_command(
            'find',
            binding=binding,
            summary=f'found {len(matches)} matches for {query!r}',
            payload={'query': query, 'match_count': len(matches), 'around': around, 'with_page': with_page},
        )
        warnings: list[str] = list(result.get('warnings') or []) if isinstance(result.get('warnings'), list) else []
        for match in matches:
            if isinstance(match, dict):
                for warning in match.get('warnings') or []:
                    if warning not in warnings:
                        warnings.append(warning)
        proof_payload = None
        if proof_match is not None:
            if proof_match > len(matches):
                raise LocalCliServiceError(f'No find match number {proof_match} for proof-match.', status_code=404)
            proof_payload = matches[proof_match - 1]
        return {
            'schema_version': 'local-cli/find/v2',
            'ok': True,
            'read_only': True,
            'selection_mutated': False,
            'query': query,
            'around': around,
            'with_page': with_page,
            'match_count': len(matches),
            'document_generation': result.get('document_generation'),
            'matches': matches,
            'proof_match': proof_payload,
            'warnings': warnings,
        }

    def info(self, *, target: str, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        last_find = binding.get('last_find') if isinstance(binding.get('last_find'), dict) else {}
        cached_matches = last_find.get('matches') if isinstance(last_find.get('matches'), list) else []

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            paragraphs = self._live_paragraph_records(handle, purpose='info')
            try:
                raw_target, match_number = resolve_match_target(target, cached_matches=cached_matches)
                if match_number is not None:
                    if match_number > len(cached_matches):
                        raise LocalCliServiceError(
                            f'No cached match number {match_number}. Run hwpx find first or use text.',
                            status_code=404,
                        )
                    match = dict(cached_matches[match_number - 1])
                else:
                    matches = find_matches(paragraphs, raw_target)
                    if not matches:
                        raise LocalCliServiceError(f'No match found for: {raw_target}', status_code=404)
                    match = matches[0]
                payload = build_context(paragraphs, match)
            except LocalCliDocumentError as exc:
                raise LocalCliRuntimeError(str(exc)) from exc
            return {
                'payload': payload,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='info', task_label='local_cli.info', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        payload = result.get('payload') if isinstance(result.get('payload'), dict) else {}
        binding = self._update_live_binding(binding, location=location)
        self._record_local_cli_command('info', binding=binding, summary=f'loaded info for {target!r}', payload={'target': target})
        return {'ok': True, **payload}

    def move(self, *, target: str, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        query, occurrence = self._resolve_live_target(binding, target)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            match = self._find_live_match(handle.hwp, query=query, occurrence=occurrence)
            cursor_pos = _selection_anchor_pos(match['snapshot'])
            if cursor_pos is None:
                raise LocalCliRuntimeError('Failed to resolve the live cursor position for the match.')
            _set_pos(handle.hwp, cursor_pos[0], cursor_pos[1], cursor_pos[2])
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='move', task_label='local_cli.move', handler=_handler)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location)
        summary = f'moved to match {occurrence} for {query!r}'
        self._record_local_cli_command('move', binding=binding, summary=summary, payload={'target': target, 'query': query, 'occurrence': occurrence})
        return {
            'ok': True,
            'summary': summary,
            'caret_pos': snapshot.get('pos'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def select(self, *, target: str, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        query, occurrence = self._resolve_live_target(binding, target)
        numbered_target = str(target or '').strip().isdigit()

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            ambiguity_warning = ''
            duplicate_match_count = None
            if not numbered_target:
                try:
                    duplicate_match_count = len(find_matches(self._live_paragraph_records(handle, purpose='select-ambiguity'), query, limit=2))
                    if duplicate_match_count > 1:
                        ambiguity_warning = 'Target text is not unique; selected the first live match. Run `hwpx find` and then `hwpx select <number>` for an unambiguous target.'
                except Exception:
                    duplicate_match_count = None
            match = self._find_live_match(handle.hwp, query=query, occurrence=occurrence)
            match_snapshot = match.get('snapshot') if isinstance(match.get('snapshot'), dict) else _snapshot_cursor_context(handle.hwp)
            selected = {
                'selected_text': match.get('selected_text') or '',
                'selected_text_normalized': match.get('selected_text_normalized') or _normalize_visible_text(match.get('selected_text') or ''),
            }
            selected_pos = match_snapshot.get('selected_pos')
            pre_location_selection_proof = self._verify_select_live_selection(
                handle.hwp,
                selected_range=selected_pos,
                selected_text=str(selected.get('selected_text') or ''),
                query=query,
                match_safe_for_type=bool(match.get('safe_for_type')),
            )
            # `snapshot_live_location()` normally captures nearby text, but that
            # path selects paragraphs internally and restores only the caret. For
            # `hwpx select`, preserving the live selection is the proof contract,
            # so use a non-invasive location snapshot and verify the range again.
            location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
                include_nearby_context=False,
            )
            selection_proof = self._verify_select_live_selection(
                handle.hwp,
                selected_range=selected_pos,
                selected_text=str(selected.get('selected_text') or ''),
                query=query,
                match_safe_for_type=bool(match.get('safe_for_type')),
            )
            if selection_proof.get('restore_attempted'):
                location = snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                    include_nearby_context=False,
                )
            final_snapshot = selection_proof.get('snapshot_final') if isinstance(selection_proof.get('snapshot_final'), dict) else match_snapshot
            return {
                'snapshot': final_snapshot,
                'selected': selected,
                'selection_proof': selection_proof,
                'pre_location_selection_proof': pre_location_selection_proof,
                'match_strategy': match.get('match_strategy'),
                'matched_query': match.get('matched_query'),
                'safe_for_type': bool(selection_proof.get('safe_for_type')),
                'warning': '; '.join(item for item in (match.get('warning'), ambiguity_warning) if item),
                'duplicate_match_count': duplicate_match_count,
                'numbered_target': numbered_target,
                'location': location,
            }

        result = self._execute_live(binding=binding, command_name='select', task_label='local_cli.select', handler=_handler)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        selected = result.get('selected') if isinstance(result.get('selected'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        safe_for_type = bool(result.get('safe_for_type'))
        warning = str(result.get('warning') or '')
        selection_proof = result.get('selection_proof') if isinstance(result.get('selection_proof'), dict) else {}
        active_selection_verified = bool(selection_proof.get('active_selection_verified'))
        selection_status = str(selection_proof.get('selection_status') or ('active' if active_selection_verified else 'degraded'))
        degraded_reason = str(selection_proof.get('degraded_reason') or '')
        proof_warnings = [str(item) for item in selection_proof.get('warnings') or [] if str(item)] if isinstance(selection_proof.get('warnings'), list) else []
        combined_warning = '; '.join(item for item in (warning, *proof_warnings) if item)
        binding = self._update_live_binding(binding, location=location)
        selected_range = snapshot.get('selected_pos')
        selected_text_proof = selected.get('selected_text_normalized') or selected.get('selected_text') or ''
        has_selected_text_proof = bool(_normalize_visible_text(selected_text_proof))
        has_restorable_range = bool(isinstance(selected_range, list) and selected_range and selected_range[0])
        if safe_for_type and active_selection_verified and has_selected_text_proof:
            binding['selected_range'] = list(selected_range) if has_restorable_range else None
            binding['last_selection'] = {
                'query': query,
                'occurrence': occurrence,
                'selected_text': selected_text_proof,
                'selected_text_hash': self._text_proof_hash(selected_text_proof),
                'selected_range': list(selected_range) if has_restorable_range else None,
                'proof_source': 'hwpx select',
                'proof_method': 'native find + pyhwpx.get_selected_text(keep_select=True) + select_text(range restore)',
                'match_strategy': result.get('match_strategy'),
                'selection_status': selection_status,
                'active_selection_verified': active_selection_verified,
                'safe_for_type': safe_for_type,
                'warning': combined_warning or None,
                'updated_at': utc_now_iso(),
            }
            binding['unsafe_selection_for_type'] = None
            binding = self._save_binding(binding)
        else:
            binding['selected_range'] = None
            binding['last_selection'] = {
                'query': query,
                'occurrence': occurrence,
                'selected_text': selected_text_proof,
                'selected_text_hash': self._text_proof_hash(selected_text_proof) if has_selected_text_proof else None,
                'selected_range': list(selected_range) if has_restorable_range else None,
                'proof_source': 'hwpx select (degraded cached proof only)',
                'proof_method': selection_proof.get('proof_method') or 'native find + live get_selected_pos verification',
                'match_strategy': result.get('match_strategy'),
                'selection_status': selection_status,
                'active_selection_verified': active_selection_verified,
                'safe_for_type': False,
                'warning': combined_warning or degraded_reason or None,
                'updated_at': utc_now_iso(),
            } if (has_selected_text_proof or has_restorable_range) else None
            binding['unsafe_selection_for_type'] = combined_warning or degraded_reason or 'The selected range is an anchor/location proof only and is not safe for hwpx type.'
            binding = self._save_binding(binding)
        if active_selection_verified and safe_for_type:
            summary = f'selected match {occurrence} for {query!r}'
        elif active_selection_verified:
            summary = f'found match {occurrence} for {query!r}; live selection active but not safe for hwpx type'
        else:
            summary = f'found match {occurrence} for {query!r}, but active selection was not preserved'
        self._record_local_cli_command(
            'select',
            binding=binding,
            summary=summary,
            payload={
                'target': target,
                'query': query,
                'occurrence': occurrence,
                'match_strategy': result.get('match_strategy'),
                'safe_for_type': safe_for_type,
                'selection_status': selection_status,
                'active_selection_verified': active_selection_verified,
                'degraded_reason': degraded_reason or None,
                'selected_range': list(selected_range) if has_restorable_range else None,
                'selected_text_preview': _preview_text(selected_text_proof, limit=120),
                'selected_text_hash': self._text_proof_hash(selected_text_proof) if has_selected_text_proof else None,
                'duplicate_match_count': result.get('duplicate_match_count'),
                'numbered_target': result.get('numbered_target'),
                'warning': combined_warning or None,
            },
        )
        return {
            'ok': True,
            'summary': summary,
            'selected_text': selected.get('selected_text_normalized') or selected.get('selected_text') or '',
            'selected_pos': snapshot.get('selected_pos'),
            'selected_text_hash': self._text_proof_hash(selected_text_proof) if has_selected_text_proof else None,
            'selected_text_len': len(str(selected.get('selected_text') or '')),
            'proof_method': selection_proof.get('proof_method') or 'native find + live get_selected_pos verification',
            'selection_proof': selection_proof,
            'selection_status': selection_status,
            'active_selection_verified': active_selection_verified,
            'degraded_reason': degraded_reason or None,
            'match_strategy': result.get('match_strategy'),
            'safe_for_type': safe_for_type,
            'duplicate_match_count': result.get('duplicate_match_count'),
            'numbered_target': result.get('numbered_target'),
            'warning': combined_warning or None,
            **self._compact_state_payload(location=location),
        }

    def replace(self, *, target: str, text: str, session_id: str | None = None) -> dict[str, Any]:
        raise LocalCliServiceError(
            'atomic replace is temporarily disabled: live HWP validation showed the current delete+insert strategy can corrupt page flow. '
            'Use select/type only with page-count proof, or implement a Hancom-native overwrite strategy in a disposable test document first.',
            status_code=501,
        )
        binding = self._load_active_binding(session_id=session_id)
        target = self._validate_single_paragraph_text(value=target, field_name='replace target', command_name='replace')
        text = self._validate_single_paragraph_text(value=text, field_name='replacement text', command_name='replace')
        query, occurrence = self._resolve_live_target(binding, target)
        if '\n' in query or '\r' in query:
            raise LocalCliServiceError(
                'replace target must be a single paragraph. Multi-paragraph exact replace is not supported yet.',
                status_code=400,
            )
        if not query:
            raise LocalCliServiceError('replace target must not be empty', status_code=400)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            match = self._find_live_match(handle.hwp, query=query, occurrence=occurrence)
            match_snapshot = match.get('snapshot') if isinstance(match.get('snapshot'), dict) else {}
            selected_range = self._normalize_selected_range(match_snapshot.get('selected_pos'))
            if selected_range is None:
                raise LocalCliRuntimeError('Replace target was found, but no selectable text range was produced; refusing to type at caret.')

            # `_find_live_match()` leaves the found text selected. Avoid
            # context-capture or extra select_text calls here: on some Hancom /
            # pyhwpx builds those operations can collapse the live selection and
            # turn a replacement into an insertion risk. Atomic replace must
            # fail closed before deletion if that live selection is gone.
            proof_snapshot = _snapshot_cursor_context(handle.hwp)
            if not bool(proof_snapshot.get('has_selection')):
                raise LocalCliRuntimeError('Replacement selection proof failed: no active selection after finding the target.')

            selected_text = str(
                match.get('selected_text_normalized')
                or match.get('selected_text')
                or ''
            )
            if not self._replace_selection_proof_ok(query=query, selected_text=selected_text, context={}):
                raise LocalCliRuntimeError(
                    'Replacement selection proof failed: selected text did not contain the expected target; refusing to modify.'
                )

            delete_snapshot = _snapshot_cursor_context(handle.hwp)
            if not bool(delete_snapshot.get('has_selection')):
                raise LocalCliRuntimeError('Replacement selection proof failed: selection was lost before deletion; refusing to modify.')

            try:
                _delete_selection(handle.hwp)
            except EditOperationError as exc:
                raise LocalCliRuntimeError(f'Failed to delete the replacement selection: {exc}') from exc
            insert_text_at_caret(handle.hwp, text)
            after_snapshot = _snapshot_cursor_context(handle.hwp)
            after_context = _capture_nearby_text_context(handle.hwp)
            return {
                'before': proof_snapshot,
                'before_selected_text': selected_text,
                'selected_pos': list(selected_range),
                'mode': 'atomic-replace',
                'strategy': 'find+select+proof+delete+insert_text',
                'native_undo_steps': 1 + _native_type_action_count(text),
                'snapshot': after_snapshot,
                'context': after_context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='replace', task_label='local_cli.replace', handler=_handler)
        before = result.get('before') if isinstance(result.get('before'), dict) else {}
        mode = str(result.get('mode') or 'atomic-replace')
        strategy = str(result.get('strategy') or '')
        native_undo_steps = int(result.get('native_undo_steps') or 1)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        binding['pending_logical_undo_count'] = max(1, native_undo_steps)
        binding['selected_range'] = None
        binding['last_selection'] = None
        binding = self._save_binding(binding)
        replaced_text = str(result.get('before_selected_text') or '')
        target_preview = _preview_text(query, limit=40)
        replaced_preview = _preview_text(replaced_text, limit=40)
        replacement_length = len(text)
        after_preview = str(context.get('current_paragraph_preview') or '').strip()
        summary = (
            f"atomic-replace {_preview_text(target_preview, limit=40)!r} "
            f"with {replacement_length} chars; after: {_preview_text(after_preview, limit=80)!r}"
        )
        self._record_local_cli_command(
            'replace',
            binding=binding,
            summary=summary,
            payload={
                'target': query,
                'target_arg': target,
                'occurrence': occurrence,
                'text': text,
                'mode': mode,
                'strategy': strategy or None,
                'native_undo_steps': native_undo_steps,
                'target_preview': target_preview,
                'replacement_length': replacement_length,
                'replaced_text_preview': replaced_preview,
                'before': before.get('pos'),
                'before_selected_pos': before.get('selected_pos'),
                'after': snapshot.get('pos'),
                'after_preview': after_preview,
            },
        )
        return {
            'ok': True,
            'summary': summary,
            'mode': mode,
            'strategy': strategy or None,
            'native_undo_steps': native_undo_steps,
            'target_preview': target_preview,
            'replacement_length': replacement_length,
            'replaced_text_preview': replaced_preview,
            'replaced_text': replaced_text,
            'caret_pos': snapshot.get('pos'),
            'context': context,
        }

    def cell(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            self._select_current_cell(handle.hwp)
            snapshot = _snapshot_cursor_context(handle.hwp)
            selected = _capture_selected_text_snapshot(handle.hwp)
            return {
                'snapshot': snapshot,
                'selected': selected,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='cell', task_label='local_cli.cell', handler=_handler)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        selected = result.get('selected') if isinstance(result.get('selected'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location)
        summary = f"selected current cell {snapshot.get('cell_addr') or '?'} at the caret position"
        self._record_local_cli_command('cell', binding=binding, summary=summary, payload={'cell_addr': snapshot.get('cell_addr')})
        return {
            'ok': True,
            'summary': summary,
            'cell_addr': snapshot.get('cell_addr'),
            'selected_text': selected.get('selected_text_normalized') or selected.get('selected_text') or '',
            **self._compact_state_payload(location=location),
        }

    def cell_move(self, *, direction: str, count: int, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            self._run_cell_move(handle.hwp, direction=direction, count=count)
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='cellmove', task_label='local_cli.cellmove', handler=_handler)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location)
        summary = f'moved current cell selection {str(direction).strip().lower()} x{count}'
        self._record_local_cli_command('cellmove', binding=binding, summary=summary, payload={'direction': direction, 'count': count, 'cell_addr': snapshot.get('cell_addr')})
        return {
            'ok': True,
            'summary': summary,
            'cell_addr': snapshot.get('cell_addr'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def cursor_move(self, *, direction: str, count: int, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            self._run_caret_move(handle.hwp, direction=direction, count=count)
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='cursormove', task_label='local_cli.cursormove', handler=_handler)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location)
        summary = f'moved caret {str(direction).strip().lower()} x{count}'
        self._record_local_cli_command('cursormove', binding=binding, summary=summary, payload={'direction': direction, 'count': count})
        return {
            'ok': True,
            'summary': summary,
            'caret_pos': snapshot.get('pos'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def _validate_single_paragraph_text(self, *, value: str, field_name: str, command_name: str) -> str:
        if not isinstance(value, str) or not value:
            raise LocalCliServiceError(f'{field_name} must not be empty', status_code=400)
        if '\n' in value or '\r' in value:
            raise LocalCliServiceError(
                f'{field_name} must be a single paragraph. Multiline {command_name} is disabled until live Hancom layout-safe paragraph insertion is implemented.',
                status_code=400,
            )
        return value

    def _replace_selection_proof_ok(
        self,
        *,
        query: str,
        selected_text: str,
        context: dict[str, Any],
    ) -> bool:
        if _selected_text_contains_probe(selected_text, query):
            return True

        normalized_query = _normalize_visible_text(query)
        if normalized_query and _selected_text_contains_probe(selected_text, normalized_query):
            return True

        context_values = [
            str(context.get('previous_paragraph_preview') or ''),
            str(context.get('current_paragraph_preview') or ''),
            str(context.get('next_paragraph_preview') or ''),
        ]
        for context_value in context_values:
            if _selected_text_contains_probe(context_value, query):
                return True
            if normalized_query and _selected_text_contains_probe(context_value, normalized_query):
                return True

        tokens = [token for token in normalized_query.split() if len(token) >= 2]
        if tokens and all(_selected_text_contains_probe(selected_text, token) for token in tokens):
            return True
        if tokens and any(
            all(_selected_text_contains_probe(context_value, token) for token in tokens)
            for context_value in context_values
        ):
                return True
        return False

    def _resolve_cell_replace_body(
        self,
        *,
        text: str | None,
        text_file: str | None,
    ) -> tuple[str, str | None]:
        if text is not None:
            body = str(text)
            if not body:
                raise LocalCliServiceError('cell-replace text must not be empty', status_code=400)
            return body, str(text_file or '').strip() or None

        source = str(text_file or '').strip()
        if not source:
            raise LocalCliServiceError('cell-replace requires text or text_file.', status_code=400)
        path = Path(source).expanduser()
        if not path.exists() or not path.is_file():
            raise LocalCliServiceError(f'cell-replace text_file not found on the server: {source}', status_code=400)
        try:
            body = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            body = path.read_text(encoding='utf-8-sig')
        if not body:
            raise LocalCliServiceError('cell-replace text_file is empty.', status_code=400)
        return body, str(path)

    def _text_proof_hash(self, value: str) -> str:
        normalized = _normalize_visible_text(value)
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]

    def _target_identity_from_snapshot(
        self,
        *,
        kind: str,
        snapshot: dict[str, Any] | None,
        page_evidence: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        identity: dict[str, Any] = {
            'kind': kind,
            'page': (page_evidence or {}).get('page'),
            'page_evidence': page_evidence or None,
            'pos': snapshot.get('pos'),
            'selected_pos': snapshot.get('selected_pos'),
            'field_name': snapshot.get('field_name'),
            'cell_addr': snapshot.get('cell_addr'),
            'cell_ref': snapshot.get('cell_ref'),
            'is_cell': snapshot.get('is_cell'),
            'selection_mode': snapshot.get('selection_mode'),
        }
        if extra:
            identity.update(extra)
        return {key: value for key, value in identity.items() if value not in (None, '')}

    def _build_raw_target_readback(
        self,
        *,
        session_root: Path,
        operation_id: str,
        raw_text: str,
        intended_text: str | None,
        target_identity: dict[str, Any],
        fallback_transform_log: list[dict[str, Any]] | None = None,
        fail_on_mismatch: bool = False,
    ) -> dict[str, Any]:
        try:
            return build_raw_target_readback(
                session_root=session_root,
                operation_id=operation_id,
                raw_text=raw_text,
                intended_text=intended_text,
                target_identity=target_identity,
                fallback_transform_log=fallback_transform_log or [],
                fail_on_mismatch=fail_on_mismatch,
            )
        except RawReadbackMismatch as exc:
            raise LocalCliRuntimeError(str(exc)) from exc

    def _capture_set_text_file_target_readback(
        self,
        hwp: Any,
        *,
        session_root: Path,
        intended_text: str,
        before_snapshot: dict[str, Any] | None,
        after_snapshot: dict[str, Any] | None,
        insert_strategy: dict[str, Any],
        fail_on_mismatch: bool | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        warnings: list[str] = []
        after_snapshot = after_snapshot if isinstance(after_snapshot, dict) else {}
        before_snapshot = before_snapshot if isinstance(before_snapshot, dict) else {}
        page_evidence = self._bundle_page_evidence(hwp)
        target_snapshot = after_snapshot or before_snapshot
        cell_addr = str(target_snapshot.get('cell_addr') or '').strip().upper()
        raw_text = ''
        target_kind = 'current-paragraph'
        strict = bool(fail_on_mismatch) if fail_on_mismatch is not None else False
        try:
            if cell_addr:
                _select_current_cell_contents(hwp, expected_cell_addr=cell_addr)
                proof_snapshot = _snapshot_cursor_context(hwp)
                raw_text = _get_selected_text(hwp, keep_select=True)
                target_snapshot = proof_snapshot
                target_kind = 'table-cell'
                if fail_on_mismatch is None:
                    strict = True
            else:
                raw_text = _get_current_paragraph_text_at_cursor(hwp)
                target_kind = 'current-paragraph'
                warnings.append(
                    'raw target readback for generic set_text_file is limited to the current paragraph; '
                    'use cell-replace or an exact selection/cell proof for a fail-closed multiline gate.'
                )
        except Exception as exc:
            warnings.append(f'raw target readback unavailable after set_text_file: {type(exc).__name__}: {exc}')
            return None, warnings

        readback = self._build_raw_target_readback(
            session_root=session_root,
            operation_id='set_text_file',
            raw_text=raw_text,
            intended_text=intended_text,
            target_identity=self._target_identity_from_snapshot(
                kind=target_kind,
                snapshot=target_snapshot,
                page_evidence=page_evidence,
                extra={'readback_scope': target_kind},
            ),
            fallback_transform_log=[
                {'stage': 'insert', **{key: value for key, value in insert_strategy.items() if value is not None}},
                {'stage': 'readback', 'scope': target_kind, 'strict': strict},
            ],
            fail_on_mismatch=strict,
        )
        return readback, warnings

    def _cell_text_contains(self, haystack: str, needle: str | None) -> bool:
        probe = str(needle or '').strip()
        if not probe:
            return True
        if _selected_text_contains_probe(haystack, probe):
            return True
        normalized_probe = _normalize_visible_text(probe)
        return bool(normalized_probe and _selected_text_contains_probe(haystack, normalized_probe))

    def _insert_text_file_at_caret(
        self,
        hwp: Any,
        *,
        text: str,
        session_root: Path,
    ) -> dict[str, Any]:
        del session_root  # retained for API compatibility and evidence call sites
        if '\n' in text or '\r' in text:
            try:
                return insert_multiline_text_at_caret_native(hwp, text)
            except Exception as exc:
                raise LocalCliRuntimeError(
                    'Hancom-native multiline cell insertion failed. '
                    'The set_text_file/SetTextFile insertfile fallback is disabled because it can import '
                    f'cold section/column controls into fixed forms: {type(exc).__name__}: {exc}'
                ) from exc

        try:
            insert_text_at_caret(hwp, text)
            return {
                'strategy': 'insert_text_at_caret',
                'method': 'insert_text/InsertText',
                'attempt_mode': 'single-paragraph-native-typing',
                'line_count': 1,
                'paragraph_break_count': 0,
                'file_import_used': False,
            }
        except Exception as exc:
            raise LocalCliRuntimeError(
                f'Hancom-native single-paragraph cell insertion failed: {type(exc).__name__}: {exc}'
            ) from exc

    def cell_replace(
        self,
        *,
        anchor: str | None = None,
        cell: str | None = None,
        text: str | None = None,
        text_file: str | None = None,
        expect_cell: str | None = None,
        expect_old: str | None = None,
        expect_new: str | None = None,
        expected_page: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        anchor = str(anchor or '').strip() or None
        requested_cell_addr = str(cell or '').strip().upper() or None
        expected_cell_addr = str(expect_cell or requested_cell_addr or '').strip().upper() or None
        if not anchor and not requested_cell_addr:
            raise LocalCliServiceError('cell-replace requires --anchor or --cell.', status_code=400)
        for label, value in (('cell', requested_cell_addr), ('expect_cell', expected_cell_addr)):
            if value is not None and not re.fullmatch(r'[A-Z]+[0-9]+', value):
                raise LocalCliServiceError(f'cell-replace {label} must look like A1, B2, etc.', status_code=400)
        if expected_page is not None and (isinstance(expected_page, bool) or int(expected_page) <= 0):
            raise LocalCliServiceError('cell-replace expected_page must be a positive integer when supplied', status_code=400)
        expected_old = str(expect_old or '').strip() or None
        expected_new = str(expect_new or '').strip() or None
        body, body_source = self._resolve_cell_replace_body(text=text, text_file=text_file)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            match: dict[str, Any] = {}
            if anchor:
                assert anchor is not None
                match = self._find_live_match(handle.hwp, query=anchor, occurrence=1)
                anchor_snapshot = _snapshot_cursor_context(handle.hwp)
                if requested_cell_addr:
                    try:
                        scoped_resolution = _resolve_current_table_cell_for_replacement(handle.hwp, requested_cell_addr)
                    except EditOperationError as exc:
                        raise LocalCliRuntimeError(
                            f'cell-replace could not resolve requested cell {requested_cell_addr!r} within the anchor table: {exc}'
                        ) from exc
                    anchor_snapshot = scoped_resolution['target_snapshot']
                    match = {
                        **match,
                        'requested_cell_addr': requested_cell_addr,
                        'cell_scope_resolution': scoped_resolution,
                        'match_strategy': 'anchor_then_current_table_scoped_cell_navigation',
                    }
            elif requested_cell_addr:
                try:
                    scoped_resolution = _resolve_current_table_cell_for_replacement(handle.hwp, requested_cell_addr)
                except EditOperationError as exc:
                    raise LocalCliRuntimeError(
                        f'cell-replace --cell {requested_cell_addr!r} requires the caret to already be inside the intended table; '
                        f'refusing global generated-field navigation: {exc}'
                    ) from exc
                anchor_snapshot = scoped_resolution['target_snapshot']
                match = scoped_resolution
            cell_addr = anchor_snapshot.get('cell_addr')
            if anchor_snapshot.get('is_cell') is not True:
                raise LocalCliRuntimeError(f'cell-replace target resolved, but the caret is not inside a table cell: {anchor_snapshot}')
            if expected_cell_addr is not None and cell_addr != expected_cell_addr:
                raise LocalCliRuntimeError(
                    f'cell-replace expected cell {expected_cell_addr!r} but target resolved to {cell_addr!r}'
                )
            if not cell_addr:
                raise LocalCliRuntimeError('cell-replace could not determine the current table cell address.')
            page_evidence = self._bundle_page_evidence(handle.hwp)
            if expected_page is not None and page_evidence.get('page') is not None and int(page_evidence.get('page')) != int(expected_page):
                raise LocalCliRuntimeError(
                    f'cell-replace expected page {expected_page} but target resolved to page {page_evidence.get("page")}; evidence={page_evidence}'
                )

            try:
                _select_current_cell_contents(handle.hwp, expected_cell_addr=str(cell_addr))
                selected_snapshot = _snapshot_cursor_context(handle.hwp)
                before_text = _get_selected_text(handle.hwp, keep_select=True)
            except EditOperationError as exc:
                raise LocalCliRuntimeError(f'Failed to select current cell contents before replacement: {exc}') from exc

            if selected_snapshot.get('is_cell') is not True:
                raise LocalCliRuntimeError(f'cell-replace selection is not a table cell selection: {selected_snapshot}')
            if selected_snapshot.get('cell_addr') != cell_addr:
                raise LocalCliRuntimeError(
                    f'cell-replace selection moved from cell {cell_addr!r} to {selected_snapshot.get("cell_addr")!r}'
                )
            if selected_snapshot.get('selection_mode') not in {3, 19} and not bool(selected_snapshot.get('has_selection')):
                raise LocalCliRuntimeError(f'cell-replace could not prove a live cell selection: {selected_snapshot}')
            if len(before_text) > 100000:
                raise LocalCliRuntimeError('cell-replace selected more than 100,000 characters; refusing obvious overselection.')
            if expected_old is not None and not self._cell_text_contains(before_text, expected_old):
                raise LocalCliRuntimeError('cell-replace expect_old token was not present in the selected cell text; refusing to modify.')

            control_map_before = self._capture_control_map_signature(handle.hwp)

            try:
                clear_result = _clear_current_cell_text(handle.hwp, expected_cell_addr=str(cell_addr))
            except EditOperationError as exc:
                raise LocalCliRuntimeError(f'Failed to clear current cell contents: {exc}') from exc

            insert_strategy = self._insert_text_file_at_caret(handle.hwp, text=body, session_root=handle.session_root)

            try:
                _select_current_cell_contents(handle.hwp, expected_cell_addr=str(cell_addr))
                after_snapshot = _snapshot_cursor_context(handle.hwp)
                after_text = _get_selected_text(handle.hwp, keep_select=True)
            except EditOperationError as exc:
                raise LocalCliRuntimeError(f'Failed to select current cell contents after replacement: {exc}') from exc

            if expected_old is not None and self._cell_text_contains(after_text, expected_old):
                raise LocalCliRuntimeError('cell-replace post-proof failed: expect_old token is still present after replacement.')
            if expected_new is not None and not self._cell_text_contains(after_text, expected_new):
                raise LocalCliRuntimeError('cell-replace post-proof failed: expect_new token is absent after replacement.')

            control_map_after = self._capture_control_map_signature(handle.hwp)
            control_map_assertion = self._assert_control_map_unchanged(
                before=control_map_before,
                after=control_map_after,
                operation=f'cell-replace {cell_addr}',
            )

            raw_target_readback = self._build_raw_target_readback(
                session_root=handle.session_root,
                operation_id=f'cell-replace-{cell_addr}',
                raw_text=after_text,
                intended_text=body,
                target_identity=self._target_identity_from_snapshot(
                    kind='table-cell',
                    snapshot=after_snapshot,
                    page_evidence=page_evidence,
                    extra={'anchor': anchor, 'requested_cell_addr': requested_cell_addr, 'expect_cell': expected_cell_addr},
                ),
                fallback_transform_log=[
                    {'stage': 'clear', 'result': clear_result},
                    {'stage': 'insert', **{key: value for key, value in insert_strategy.items() if value is not None}},
                    {'stage': 'readback', 'scope': 'table-cell', 'strict': True},
                ],
                fail_on_mismatch=True,
            )

            context = _capture_nearby_text_context(handle.hwp)
            return {
                'match': match,
                'cell_addr': cell_addr,
                'anchor_snapshot': anchor_snapshot,
                'page_evidence': page_evidence,
                'selected_snapshot': selected_snapshot,
                'clear_result': clear_result,
                'after_snapshot': after_snapshot,
                'before_text': before_text,
                'after_text': after_text,
                'raw_target_readback': raw_target_readback,
                'insert_strategy': insert_strategy,
                'control_map_assertion': control_map_assertion,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(
            binding=binding,
            command_name='cell-replace',
            task_label='local_cli.cell_replace',
            handler=_handler,
            timeout=120.0,
        )
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        binding['pending_logical_undo_count'] = 2
        binding['last_selection'] = None
        binding['selected_range'] = None
        binding = self._save_binding(binding)

        before_text = str(result.get('before_text') or '')
        after_text = str(result.get('after_text') or '')
        cell_addr = result.get('cell_addr')
        match = result.get('match') if isinstance(result.get('match'), dict) else {}
        insert_strategy = result.get('insert_strategy') if isinstance(result.get('insert_strategy'), dict) else {}
        raw_target_readback = result.get('raw_target_readback') if isinstance(result.get('raw_target_readback'), dict) else None
        warnings: list[str] = []
        if body_source:
            warnings.append(f'text source: {body_source}')
        if raw_target_readback and raw_target_readback.get('raw_text_path'):
            warnings.append(f'raw after_text: {raw_target_readback.get("raw_text_path")}')
        summary = f"cell-replace {cell_addr or '?'} via {insert_strategy.get('strategy') or 'unknown'}; {len(before_text)} chars -> {len(after_text)} chars"
        payload = {
            'anchor': anchor,
            'match': match,
            'cell_addr': cell_addr,
            'expect_cell': expected_cell_addr,
            'expect_old': expected_old,
            'expect_new': expected_new,
            'page_evidence': result.get('page_evidence') if isinstance(result.get('page_evidence'), dict) else {},
            'selected_snapshot': result.get('selected_snapshot') if isinstance(result.get('selected_snapshot'), dict) else {},
            'after_snapshot': result.get('after_snapshot') if isinstance(result.get('after_snapshot'), dict) else {},
            'before_preview': _preview_text(before_text, limit=120),
            'before_hash': self._text_proof_hash(before_text),
            'after_preview': _preview_text(after_text, limit=120),
            'after_hash': self._text_proof_hash(after_text),
            'after_line_count': raw_target_readback.get('line_count') if raw_target_readback else None,
            'after_raw_sha256': raw_target_readback.get('raw_sha256') if raw_target_readback else None,
            'after_raw_text_path': raw_target_readback.get('raw_text_path') if raw_target_readback else None,
            'after_raw_manifest_path': raw_target_readback.get('manifest_path') if raw_target_readback else None,
            'raw_target_readback': raw_target_readback,
            'insert_strategy': insert_strategy,
            'control_map_assertion': result.get('control_map_assertion') if isinstance(result.get('control_map_assertion'), dict) else {},
            'warnings': warnings,
        }
        self._record_local_cli_command('cell-replace', binding=binding, summary=summary, payload=payload)
        return {
            'ok': True,
            'summary': summary,
            **payload,
            'context': result.get('context') if isinstance(result.get('context'), dict) else {},
            **self._compact_state_payload(location=location, context=result.get('context') if isinstance(result.get('context'), dict) else {}),
        }

    def type_text(self, *, text: str, session_id: str | None = None, allow_insert_at_caret: bool = False) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        text = self._validate_single_paragraph_text(value=text, field_name='type text', command_name='type')

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            before = _snapshot_cursor_context(handle.hwp)
            # Do not call get_selected_text() before replacement. On the live
            # Hancom/pyhwpx stack that can collapse or widen the active selection,
            # turning a replacement into an insertion/duplication. Use the native
            # selected-position snapshot as the source of truth and only inspect
            # nearby text after the edit has completed.
            before_selected = {}
            before_selected_text = ''
            had_selection = bool(before.get('has_selection'))
            restored_cached_selection = False
            last_selection = binding.get('last_selection') if isinstance(binding.get('last_selection'), dict) else {}
            unsafe_selection_reason = str(binding.get('unsafe_selection_for_type') or '').strip()
            if had_selection and unsafe_selection_reason:
                raise LocalCliRuntimeError(
                    f'Active selection is not safe for hwpx type: {unsafe_selection_reason}'
                )
            if had_selection:
                cached_range = self._normalize_selected_range(binding.get('selected_range'))
                if cached_range is None:
                    cached_range = self._normalize_selected_range(last_selection.get('selected_range'))
                if cached_range is not None and self._selected_ranges_equal(before.get('selected_pos'), cached_range):
                    before_selected_text = str(last_selection.get('selected_text') or '')
            if not had_selection and not allow_insert_at_caret:
                stored_range = self._normalize_selected_range(binding.get('selected_range'))
                if stored_range is not None:
                    try:
                        _select_text(handle.hwp, stored_range)
                        before = _snapshot_cursor_context(handle.hwp)
                        had_selection = bool(before.get('has_selection'))
                        if had_selection:
                            before_selected_text = str(last_selection.get('selected_text') or '')
                            restored_cached_selection = True
                    except EditOperationError:
                        had_selection = False
            guard_reason = type_insert_guard_reason(
                binding,
                had_selection=had_selection,
                restored_cached_selection=restored_cached_selection,
                allow_insert_at_caret=allow_insert_at_caret,
            )
            if guard_reason:
                raise LocalCliRuntimeError(guard_reason)
            if had_selection:
                try:
                    _delete_selection(handle.hwp)
                except EditOperationError as exc:
                    raise LocalCliRuntimeError(f'Failed to delete the active selection before typing: {exc}') from exc
                insert_text_at_caret(handle.hwp, text)
                replace_strategy = {'strategy': 'Delete+insert_text', 'native_undo_steps': 1 + _native_type_action_count(text)}
            else:
                insert_text_at_caret(handle.hwp, text)
                replace_strategy = {'strategy': 'insert_text_at_caret', 'native_undo_steps': _native_type_action_count(text)}
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'before': before,
                'before_selected': before_selected,
                'before_selected_text': before_selected_text,
                'mode': 'replace-selection' if had_selection else 'insert-at-caret',
                'strategy': replace_strategy.get('strategy') if isinstance(replace_strategy, dict) else None,
                'native_undo_steps': replace_strategy.get('native_undo_steps') if isinstance(replace_strategy, dict) else 1,
                'restored_cached_selection': restored_cached_selection,
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='type', task_label='local_cli.type', handler=_handler)
        before = result.get('before') if isinstance(result.get('before'), dict) else {}
        before_selected = result.get('before_selected') if isinstance(result.get('before_selected'), dict) else {}
        before_selected_text_result = str(result.get('before_selected_text') or '')
        mode = str(result.get('mode') or 'insert-at-caret')
        strategy = str(result.get('strategy') or '')
        native_undo_steps = int(result.get('native_undo_steps') or 1)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        binding['pending_logical_undo_count'] = max(1, native_undo_steps)
        binding['selected_range'] = None
        binding['last_selection'] = None
        binding = self._save_binding(binding)
        before_selected_text = str(
            before_selected.get('selected_text_normalized')
            or before_selected.get('selected_text')
            or before_selected_text_result
            or ''
        )
        after_preview = str(context.get('current_paragraph_preview') or '').strip()
        if mode == 'replace-selection':
            summary = (
                f"replaced current selection"
                f"{(' ' + repr(_preview_text(before_selected_text, limit=40))) if before_selected_text else ''} "
                f"with {len(text)} chars; after: {_preview_text(after_preview, limit=80)!r}"
            )
        else:
            summary = (
                f"typed {len(text)} chars at the current caret position; "
                f"after: {_preview_text(after_preview, limit=80)!r}"
            )
        proof = self._build_type_text_proof(
            inserted_text=text,
            before=before,
            after=snapshot,
            mode=mode,
            strategy=strategy or None,
            before_selected_text=before_selected_text,
            context=context,
            restored_cached_selection=bool(result.get('restored_cached_selection')),
            native_undo_steps=native_undo_steps,
        )
        self._record_local_cli_command(
            'type',
            binding=binding,
            summary=summary,
            payload={
                'text': text,
                'mode': mode,
                'strategy': strategy or None,
                'native_undo_steps': native_undo_steps,
                'replaced_text': before_selected_text if mode == 'replace-selection' else None,
                'before': before.get('pos'),
                'before_selected_pos': before.get('selected_pos'),
                'after': snapshot.get('pos'),
                'after_preview': after_preview,
                'proof': proof,
            },
        )
        return {
            'schema_version': 'local-cli/envelope/v1',
            'ok': True,
            'result': 'ok',
            'summary': summary,
            'where': 'Current active selection or caret position in the live Hancom working copy.',
            'how': 'Direct-backlog type route; selected text is not read immediately before typing to avoid selection collapse/widening.',
            'changed': f"{mode} via {proof.get('method')}; inserted text length {len(text)}",
            'proof': proof,
            'next': 'Run `hwpx where`/`hwpx selected-text-proof` as needed, then save and rendered proof before trusting layout.',
            'mode': mode,
            'strategy': strategy or None,
            'native_undo_steps': native_undo_steps,
            'replaced_text': before_selected_text if mode == 'replace-selection' else '',
            'caret_pos': snapshot.get('pos'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def anchor_insert(
        self,
        *,
        target: str,
        text: str,
        position: str = 'before-anchor',
        session_id: str | None = None,
    ) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        normalized_position = self._normalize_anchor_insert_position(position)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            self._ensure_active_working_copy(handle, purpose='anchor-insert')
            result = self._perform_anchor_insert(
                handle.hwp,
                target=target,
                position=normalized_position,
                text=text,
                session_root=handle.session_root,
            )
            result['location'] = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
            )
            return result

        result = self._execute_live(binding=binding, command_name='anchor-insert', task_label='local_cli.anchor_insert', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        binding['pending_logical_undo_count'] = 1
        binding = self._save_binding(binding)
        summary = f'inserted text {normalized_position} {target!r} in one live operation'
        self._record_local_cli_command('anchor-insert', binding=binding, summary=summary, payload=result)
        return {'ok': True, 'summary': summary, **result}

    async def figure_section(
        self,
        *,
        target_heading: str,
        heading: str,
        intro: str | None = None,
        caption: str | None = None,
        body: str | None = None,
        image_file: UploadFile | None = None,
        width: float | None = None,
        height: float | None = None,
        sizeoption: int | None = None,
        treat_as_char: str | bool | None = None,
        embedded: str | bool | None = None,
        fit_cell: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        target_heading = self._normalize_figure_text_field(target_heading, field_name='target_heading', required=True, max_chars=500)
        heading = self._normalize_figure_text_field(heading, field_name='heading', required=True, max_chars=500)
        intro = self._normalize_figure_text_field(intro, field_name='intro') if intro is not None else None
        caption = self._normalize_figure_text_field(caption, field_name='caption', max_chars=1000) if caption is not None else None
        body = self._normalize_figure_text_field(body, field_name='body') if body is not None else None
        staged: dict[str, Any] | None = None
        options: dict[str, Any] = {}
        try:
            if image_file is not None:
                options = self._normalize_image_options(
                    width=width,
                    height=height,
                    sizeoption=sizeoption,
                    treat_as_char=treat_as_char,
                    embedded=embedded,
                    fit_cell=fit_cell,
                )
                staged = await self._stage_image_upload(file=image_file, binding=binding)
            before_image_text, after_image_text = self._format_figure_section_text(
                heading=heading, intro=intro, caption=caption, body=body
            )
            if not before_image_text and not after_image_text:
                raise LocalCliServiceError('figure-section has no content to insert', status_code=400)

            def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
                self._ensure_active_working_copy(handle, purpose='figure-section')
                before = self._bundle_compact_snapshot(handle.hwp)
                anchor = self._move_to_anchor_insert_position(handle.hwp, target=target_heading, position='before-heading')
                text_results: list[dict[str, Any]] = []
                image_result: dict[str, Any] | None = None
                image_control: dict[str, Any] | None = None
                if before_image_text:
                    strategy = self._insert_text_file_at_caret(handle.hwp, text=before_image_text, session_root=handle.session_root)
                    text_results.append({'part': 'heading_intro', 'text_len': len(before_image_text), 'text_hash': self._text_proof_hash(before_image_text), 'strategy': strategy})
                if staged is not None:
                    image_result = self._insert_picture_with_available_method(
                        handle.hwp,
                        image_path=Path(str(staged['staged_path'])),
                        options=options,
                    )
                    image_control = self._capture_current_control_id(handle.hwp)
                if after_image_text:
                    strategy = self._insert_text_file_at_caret(handle.hwp, text=after_image_text, session_root=handle.session_root)
                    text_results.append({'part': 'caption_body', 'text_len': len(after_image_text), 'text_hash': self._text_proof_hash(after_image_text), 'strategy': strategy})
                after = self._bundle_compact_snapshot(handle.hwp)
                context = _capture_nearby_text_context(handle.hwp)
                location = snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                )
                proof = {
                    'expected_order': ['section_heading', 'intro', 'image', 'figure_caption', 'body', 'next_heading'],
                    'section_heading': heading,
                    'figure_caption': caption,
                    'next_heading': target_heading,
                    'image_present': staged is not None,
                    'text_hashes': text_results,
                }
                warnings: list[str] = []
                if image_control and image_control.get('warning'):
                    warnings.append(str(image_control.get('warning')))
                if staged is None:
                    warnings.append('No image supplied; figure-section inserted text-only heading/intro/caption/body before target heading.')
                native_undo_steps = len(text_results) + (1 if image_result is not None else 0)
                return {
                    'schema_version': 'local-cli/figure-section/v1',
                    'target_heading': target_heading,
                    'anchor': anchor,
                    'before': before,
                    'after': after,
                    'context': context,
                    'location': location,
                    'staged_image': ({**staged, 'staged_path': str(staged.get('staged_path'))} if staged is not None else None),
                    'image_insertion': image_result,
                    'image_control': image_control,
                    'proof': proof,
                    'native_undo_steps': max(1, native_undo_steps),
                    'warnings': warnings,
                }

            result = self._execute_live(binding=binding, command_name='figure-section', task_label='local_cli.figure_section', handler=_handler)
        finally:
            if image_file is not None:
                await image_file.close()

        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        # A figure-section is one operator-level transaction even when Hancom
        # records several native text/image steps; `hwpx undo` should roll back
        # the last logical bundle as one requested bundle count.
        binding['pending_logical_undo_count'] = 1
        binding['last_logical_bundle'] = {'command': 'figure-section', 'target_heading': target_heading, 'heading': heading}
        binding = self._save_binding(binding)
        summary = f'inserted figure-section before heading {target_heading!r} as one logical bundle'
        self._record_local_cli_command('figure-section', binding=binding, summary=summary, payload=result)
        return {'ok': True, 'summary': summary, **result}

    async def image_upload(
        self,
        *,
        file: UploadFile,
        width: float | None = None,
        height: float | None = None,
        sizeoption: int | None = None,
        treat_as_char: str | bool | None = None,
        embedded: str | bool | None = None,
        fit_cell: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        try:
            options = self._normalize_image_options(
                width=width,
                height=height,
                sizeoption=sizeoption,
                treat_as_char=treat_as_char,
                embedded=embedded,
                fit_cell=fit_cell,
            )
            staged = await self._stage_image_upload(file=file, binding=binding)
            staged_path = staged['staged_path']

            def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
                self._ensure_active_working_copy(handle, purpose='image')
                insertion = self._insert_picture_with_available_method(
                    handle.hwp,
                    image_path=staged_path,
                    options=options,
                )
                location = snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                )
                return {
                    'insertion': insertion,
                    'location': location,
                }

            result = self._execute_live(binding=binding, command_name='image', task_label='local_cli.image', handler=_handler)
        finally:
            await file.close()

        insertion = result.get('insertion') if isinstance(result.get('insertion'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        binding['pending_logical_undo_count'] = 1
        binding = self._save_binding(binding)

        mode = 'fit-cell' if options.get('fit_cell') else 'insert-picture'
        summary = (
            f"inserted image {staged.get('staged_filename')} via {insertion.get('method') or 'unknown'} "
            f"({mode}, sizeoption={options.get('sizeoption') if options.get('sizeoption') is not None else 'default'})"
        )
        command_payload = {
            'filename': staged.get('staged_filename'),
            'original_filename': staged.get('original_filename'),
            'size_bytes': staged.get('size_bytes'),
            'mode': mode,
            'method': insertion.get('method'),
            'attempt_mode': insertion.get('attempt_mode'),
            'options': options,
            'cursor_summary': location.get('cursor_summary'),
            'selection_summary': location.get('selection_summary'),
        }
        self._record_local_cli_command('image', binding=binding, summary=summary, payload=command_payload)
        return {
            'ok': True,
            'summary': summary,
            'filename': staged.get('staged_filename'),
            'original_filename': staged.get('original_filename'),
            'mode': mode,
            'method': insertion.get('method'),
            'attempt_mode': insertion.get('attempt_mode'),
            'options': options,
            'cursor_summary': location.get('cursor_summary'),
            'selection_summary': location.get('selection_summary'),
            'current_paragraph_preview': location.get('current_paragraph_preview'),
        }

    async def image_upload_at_anchor(
        self,
        *,
        file: UploadFile,
        target: str,
        position: str = 'before-anchor',
        width: float | None = None,
        height: float | None = None,
        sizeoption: int | None = None,
        treat_as_char: str | bool | None = None,
        embedded: str | bool | None = None,
        fit_cell: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        query = str(target or '').strip()
        if not query:
            await file.close()
            raise LocalCliServiceError('target must not be empty', status_code=400)
        normalized_position = self._normalize_anchor_insert_position(position)
        try:
            options = self._normalize_image_options(
                width=width,
                height=height,
                sizeoption=sizeoption,
                treat_as_char=treat_as_char,
                embedded=embedded,
                fit_cell=fit_cell,
            )
            staged = await self._stage_image_upload(file=file, binding=binding)
            staged_path = staged['staged_path']

            def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
                self._ensure_active_working_copy(handle, purpose='image-at-anchor')
                before = self._bundle_compact_snapshot(handle.hwp)
                anchor = self._move_to_anchor_insert_position(handle.hwp, target=query, position=normalized_position)
                insertion = self._insert_picture_with_available_method(
                    handle.hwp,
                    image_path=staged_path,
                    options=options,
                )
                image_control = self._capture_current_control_id(handle.hwp)
                after = self._bundle_compact_snapshot(handle.hwp)
                context = _capture_nearby_text_context(handle.hwp)
                location = snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                )
                warnings: list[str] = [
                    'Rendered proof must confirm image/caption pairing, no clipping, and section continuity before save/final delivery.'
                ]
                if image_control and image_control.get('warning'):
                    warnings.append(str(image_control.get('warning')))
                return {
                    'schema_version': 'local-cli/image-at-anchor/v1',
                    'target': query,
                    'position': normalized_position,
                    'anchor': anchor,
                    'before': before,
                    'after': after,
                    'context': context,
                    'location': location,
                    'staged_image': ({**staged, 'staged_path': str(staged.get('staged_path'))}),
                    'image_insertion': insertion,
                    'image_control': image_control,
                    'options': options,
                    'proof_required': 'rendered proof covering image, caption/body continuity, no clipping/overflow, and neighboring section continuity',
                    'warnings': warnings,
                }

            result = self._execute_live(binding=binding, command_name='image-at-anchor', task_label='local_cli.image_at_anchor', handler=_handler)
        finally:
            await file.close()

        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        binding['pending_logical_undo_count'] = 1
        binding['last_logical_bundle'] = {'command': 'image-at-anchor', 'target': query, 'position': normalized_position}
        binding = self._save_binding(binding)
        staged = result.get('staged_image') if isinstance(result.get('staged_image'), dict) else {}
        insertion = result.get('image_insertion') if isinstance(result.get('image_insertion'), dict) else {}
        summary = (
            f"inserted image {staged.get('staged_filename') or Path(str(staged.get('staged_path') or 'image')).name} "
            f"{normalized_position} {query!r} via {insertion.get('method') or 'unknown'}"
        )
        self._record_local_cli_command(
            'image-at-anchor',
            binding=binding,
            summary=summary,
            payload={
                'target': query,
                'position': normalized_position,
                'filename': staged.get('staged_filename'),
                'method': insertion.get('method'),
                'attempt_mode': insertion.get('attempt_mode'),
                'options': result.get('options'),
                'cursor_summary': location.get('cursor_summary'),
                'selection_summary': location.get('selection_summary'),
                'warnings': result.get('warnings'),
            },
        )
        return {
            'ok': True,
            'summary': summary,
            'filename': staged.get('staged_filename'),
            'original_filename': staged.get('original_filename'),
            'mode': 'fit-cell' if (result.get('options') or {}).get('fit_cell') else 'insert-picture',
            'method': insertion.get('method'),
            'attempt_mode': insertion.get('attempt_mode'),
            'target': query,
            'position': normalized_position,
            'anchor': result.get('anchor'),
            'options': result.get('options'),
            'cursor_summary': location.get('cursor_summary'),
            'selection_summary': location.get('selection_summary'),
            'current_paragraph_preview': location.get('current_paragraph_preview'),
            'proof_required': result.get('proof_required'),
            'warnings': result.get('warnings'),
        }

    def font_size(self, *, size_pt: float, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        if isinstance(size_pt, bool) or float(size_pt) <= 0:
            raise LocalCliServiceError('fontsize must be a positive point value', status_code=400)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            before = _snapshot_cursor_context(handle.hwp)
            style_result = apply_char_style(handle.hwp, height_pt=float(size_pt))
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'before': before,
                'style_result': style_result,
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='fontsize', task_label='local_cli.fontsize', handler=_handler)
        before = result.get('before') if isinstance(result.get('before'), dict) else {}
        style_result = result.get('style_result') if isinstance(result.get('style_result'), dict) else {}
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_selection_cache=True)
        size_label = self._style_value_label(float(size_pt))
        scope_label = self._style_scope_label(before)
        summary = (
            f'applied {size_label}pt font size to the current selection'
            if scope_label == 'current selection'
            else f'set font size to {size_label}pt at the current caret position'
        )
        proof = self._build_font_size_proof(
            requested_size_pt=float(size_pt),
            before=before,
            after=snapshot,
            style_result=style_result,
            context=context,
        )
        self._record_local_cli_command(
            'fontsize',
            binding=binding,
            summary=summary,
            payload={'size_pt': float(size_pt), 'scope': scope_label, 'strategy': style_result.get('strategy'), 'proof': proof},
        )
        return {
            'schema_version': 'local-cli/envelope/v1',
            'ok': True,
            'result': 'ok',
            'summary': summary,
            'where': f'Current active {scope_label} in the live Hancom working copy.',
            'how': 'Direct-backlog character-shape route using the live Hancom/pyhwpx style executor.',
            'changed': f'font size command requested {size_label}pt via {proof.get("method")}',
            'proof': proof,
            'next': 'Run `hwpx save` and rendered proof (`hwpx export-proof-range` or `hwpx page-screenshot`) before trusting layout.',
            'caret_pos': snapshot.get('pos'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def bold(self, *, enabled: bool, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        last_selection = binding.get('last_selection') if isinstance(binding.get('last_selection'), dict) else {}

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            before = _snapshot_cursor_context(handle.hwp)
            proof_source = 'active-selection'
            selected_range = before.get('selected_pos')
            live_reselect: dict[str, Any] = {}
            if bool(before.get('has_selection')):
                try:
                    selected_text = _get_selected_text(handle.hwp, keep_select=True)
                except EditOperationError as exc:
                    raise LocalCliRuntimeError(f'Failed to read selected-text proof before bold; no mutation performed: {exc}') from exc
                selected_text_normalized = _normalize_visible_text(selected_text)
                if not selected_text_normalized:
                    raise LocalCliRuntimeError(
                        'hwpx bold requires non-empty selected text; no mutation performed. '
                        'Run `hwpx select <target>` again and verify with `hwpx selected-text-proof`.'
                    )
                # On some Hancom/pyhwpx stacks get_selected_text(keep_select=True) still collapses
                # or expands the live selection. Restore the exact pre-proof selected range before
                # applying the documented character-shape mutation.
                self._restore_selected_range(handle.hwp, selected_range)
            else:
                selected_text = str(last_selection.get('selected_text') or '') if isinstance(last_selection, dict) else ''
                selected_text_normalized = _normalize_visible_text(selected_text)
                query = str(last_selection.get('query') or '').strip() if isinstance(last_selection, dict) else ''
                try:
                    occurrence = int(last_selection.get('occurrence') or 1) if isinstance(last_selection, dict) else 1
                except Exception:
                    occurrence = 1
                if not (query and selected_text_normalized and occurrence > 0):
                    raise LocalCliRuntimeError(
                        'hwpx bold requires an active selected-text proof; no mutation performed. '
                        'Run `hwpx select <target>` and verify the selected text before `hwpx bold on|off`.'
                    )
                find_method = getattr(handle.hwp, 'find', None)
                if not callable(find_method):
                    raise LocalCliRuntimeError('pyhwpx find is unavailable; cannot restore the proven selection for bold.')
                found_candidate = ''
                for candidate, _allow_whole_word in self._live_find_candidates(query):
                    _move_doc_begin(handle.hwp)
                    for index in range(occurrence):
                        if not find_method(candidate, direction='Forward', MatchCase=1, WholeWordOnly=0):
                            break
                        if index == occurrence - 1:
                            found_candidate = candidate
                            break
                        _move_after_selection(handle.hwp)
                    if found_candidate:
                        break
                if not found_candidate:
                    raise LocalCliRuntimeError(f'Failed to restore the proven selection for bold: no match found for {query!r}.')
                proof_source = 'cached-selected-text-proof+live-reselect'
                before = _snapshot_cursor_context(handle.hwp)
                selected_range = before.get('selected_pos')
                live_reselect = {'query': query, 'occurrence': occurrence, 'matched_query': found_candidate}
            restored = _snapshot_cursor_context(handle.hwp)
            if proof_source == 'active-selection' and not bool(restored.get('has_selection')):
                raise LocalCliRuntimeError('Selected-text proof did not preserve a restorable selection; no mutation performed.')
            document_is_modified_before = bool(getattr(handle.hwp, 'IsModified', False))
            style_result = apply_char_style(handle.hwp, bold=bool(enabled))
            after = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
            )
            document_is_modified_after = bool(location.get('document_is_modified'))
            return {
                'before': before,
                'restored': restored,
                'after': after,
                'selected_text': selected_text,
                'selected_text_normalized': selected_text_normalized,
                'proof_source': proof_source,
                'live_reselect': live_reselect,
                'style_result': style_result,
                'context': context,
                'location': location,
                'document_is_modified_before': document_is_modified_before,
                'document_is_modified_after': document_is_modified_after,
            }

        result = self._execute_live(binding=binding, command_name='bold', task_label='local_cli.bold', handler=_handler)
        before = result.get('before') if isinstance(result.get('before'), dict) else {}
        restored = result.get('restored') if isinstance(result.get('restored'), dict) else {}
        after = result.get('after') if isinstance(result.get('after'), dict) else {}
        style_result = result.get('style_result') if isinstance(result.get('style_result'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        modified_before = bool(result.get('document_is_modified_before'))
        modified_after = bool(result.get('document_is_modified_after'))
        binding = self._update_live_binding(binding, location=location, dirty=modified_after or modified_before, clear_selection_cache=True)
        selected_text = str(result.get('selected_text') or '')
        selected_preview = _preview_text(selected_text, limit=80)
        live_reselect = result.get('live_reselect') if isinstance(result.get('live_reselect'), dict) else {}
        state_change = f"document modified flag {'yes' if modified_before else 'no'} -> {'yes' if modified_after else 'no'}"
        summary = f"turned bold {'on' if enabled else 'off'} for the proven current selection"
        changed = f'{state_change}; selected text length {len(selected_text)}'
        proof = {
            'selection_required': True,
            'proof_source': result.get('proof_source') or 'active-selection',
            'live_reselect': live_reselect or None,
            'selected_text_preview': selected_preview,
            'selected_text_len': len(selected_text),
            'before_has_selection': bool(before.get('has_selection')),
            'restored_has_selection': bool(restored.get('has_selection')),
            'after_has_selection': bool(after.get('has_selection')),
            'document_is_modified_before': modified_before,
            'document_is_modified_after': modified_after,
            'method': style_result.get('strategy') or 'hwp.set_font',
            'doc_backed_api': 'pyhwpx hwp.set_font(Bold=True|False); CharShapeBold is avoided because it is a toggle',
        }
        self._record_local_cli_command(
            'bold',
            binding=binding,
            summary=summary,
            payload={
                'enabled': bool(enabled),
                'scope': 'current selection',
                'strategy': style_result.get('strategy'),
                'selected_text_len': len(selected_text),
                'document_is_modified_before': modified_before,
                'document_is_modified_after': modified_after,
            },
        )
        return {
            'schema_version': 'local-cli/envelope/v1',
            'ok': True,
            'result': 'ok',
            'summary': summary,
            'where': 'Current active selected text in the live Hancom working copy.',
            'how': 'Selection-required direct-backlog route using documented pyhwpx `hwp.set_font(Bold=True|False)`; `CharShapeBold` toggle is not used.',
            'changed': changed,
            'proof': proof,
            'next': 'Run `hwpx save` and rendered proof (`hwpx export-proof-range` or `hwpx page-screenshot`) before trusting layout.',
            'caret_pos': after.get('pos'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def font_family(self, *, face_name: str, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        normalized_face_name = str(face_name or '').strip()
        if not normalized_face_name:
            raise LocalCliServiceError('font name must not be empty', status_code=400)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            before = _snapshot_cursor_context(handle.hwp)
            style_result = apply_char_style(handle.hwp, face_name=normalized_face_name)
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'before': before,
                'style_result': style_result,
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='font', task_label='local_cli.font', handler=_handler)
        before = result.get('before') if isinstance(result.get('before'), dict) else {}
        style_result = result.get('style_result') if isinstance(result.get('style_result'), dict) else {}
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_selection_cache=True)
        scope_label = self._style_scope_label(before)
        summary = (
            f"set font to {normalized_face_name!r} for the current selection"
            if scope_label == 'current selection'
            else f"set font to {normalized_face_name!r} at the current caret position"
        )
        self._record_local_cli_command(
            'font',
            binding=binding,
            summary=summary,
            payload={'face_name': normalized_face_name, 'scope': scope_label, 'strategy': style_result.get('strategy')},
        )
        return {
            'ok': True,
            'summary': summary,
            'caret_pos': snapshot.get('pos'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def bullet(self, *, text: str, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        if not isinstance(text, str) or not text:
            raise LocalCliServiceError('bullet text must not be empty', status_code=400)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            before = _snapshot_cursor_context(handle.hwp)
            had_selection = bool(before.get('has_selection'))
            if had_selection:
                _delete_selection(handle.hwp)
            else:
                paragraph_text = _get_current_paragraph_text_at_cursor(handle.hwp)
                if paragraph_text.strip():
                    self._break_paragraph(handle.hwp)
            insert_text_at_caret(handle.hwp, text)
            after_insert = _snapshot_cursor_context(handle.hwp)
            self._apply_bullet_to_current_paragraph(handle.hwp)
            cursor_after_insert = self._normalize_cursor_pos(after_insert.get('pos'))
            if cursor_after_insert is not None:
                _set_pos(handle.hwp, cursor_after_insert[0], cursor_after_insert[1], cursor_after_insert[2])
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'had_selection': had_selection,
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='bullet', task_label='local_cli.bullet', handler=_handler)
        had_selection = bool(result.get('had_selection'))
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        summary = 'created one bullet item at the current caret position'
        self._record_local_cli_command('bullet', binding=binding, summary=summary, payload={'text': text, 'had_selection': had_selection})
        return {
            'ok': True,
            'summary': summary,
            'caret_pos': snapshot.get('pos'),
            'context': context,
            **self._compact_state_payload(location=location, context=context),
        }

    def table(self, *, cols: int, rows: int, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            create_table_at_cursor(handle.hwp, cols=cols, rows=rows)
            return {
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='table', task_label='local_cli.table', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        self._record_local_cli_command('table', binding=binding, summary=f'created table {cols}x{rows}', payload={'cols': cols, 'rows': rows})
        return {
            'ok': True,
            'cols': cols,
            'rows': rows,
            **self._compact_state_payload(location=location),
        }

    def list_items(self, *, count: int, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            result = insert_numbered_list_at_cursor(handle.hwp, count=count)
            return {
                'mode': result.get('mode'),
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='list', task_label='local_cli.list', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        mode = result.get('mode')
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        self._record_local_cli_command('list', binding=binding, summary=f'created numbered list with {count} items', payload={'count': count, 'mode': mode})
        return {
            'ok': True,
            'count': count,
            'mode': mode,
            **self._compact_state_payload(location=location),
        }

    def pycall(
        self,
        *,
        method_path: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        path, segments = self._validate_macro_path(method_path)
        cleaned_args, cleaned_kwargs = self._validate_macro_args(args or [], kwargs or {})
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            leaf = self._resolve_public_macro_leaf(handle.hwp, segments)
            mode = 'call' if callable(leaf) else 'property'
            if mode == 'property' and (cleaned_args or cleaned_kwargs):
                raise LocalCliRuntimeError('pycall property access does not accept args or kwargs')
            if callable(leaf):
                raw_result = leaf(*cleaned_args, **cleaned_kwargs)
            else:
                if not (leaf is None or isinstance(leaf, (bool, int, float, str))):
                    raise LocalCliRuntimeError('pycall property access is limited to scalar public properties')
                raw_result = leaf
            return {
                'mode': mode,
                'result_type': type(raw_result).__name__,
                'result_preview': self._macro_result_preview(raw_result),
            }

        result = self._execute_live(binding=binding, command_name='pycall', task_label='local_cli.pycall', handler=_handler)
        mode = str(result.get('mode') or 'call')
        result_type = str(result.get('result_type') or 'NoneType')
        result_preview = result.get('result_preview')
        binding = self._save_binding({**binding, 'last_find': None})
        summary = f'pycall {mode} {path} -> {result_type}'
        self._record_local_cli_command(
            'pycall',
            binding=binding,
            summary=summary,
            payload={
                'path': path,
                'mode': mode,
                'args_count': len(cleaned_args),
                'kwargs_keys': sorted(cleaned_kwargs.keys()),
                'result_type': result_type,
                'result_preview': result_preview,
                'macro_warning': 'pycall is a dev/macro escape hatch; run hwpx where/export after stateful calls before trusting layout.',
            },
        )
        return {
            'ok': True,
            'command': 'pycall',
            'mode': mode,
            'path': path,
            'result_type': result_type,
            'result_preview': result_preview,
            'warning': 'pycall is a dev/macro escape hatch; run hwpx where/export after stateful calls before trusting layout.',
            'summary': summary,
        }

    def action(self, *, action_name: str, session_id: str | None = None) -> dict[str, Any]:
        action = self._validate_action_name(action_name)
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            run = getattr(getattr(handle.hwp, 'HAction', None), 'Run', None)
            if not callable(run):
                raise LocalCliRuntimeError('HAction.Run is unavailable on this machine')
            raw_result = run(action)
            return {
                'result_type': type(raw_result).__name__,
                'result_preview': self._macro_result_preview(raw_result),
                'succeeded': raw_result is None or bool(raw_result),
            }

        result = self._execute_live(binding=binding, command_name='action', task_label='local_cli.action', handler=_handler)
        result_type = str(result.get('result_type') or 'NoneType')
        result_preview = result.get('result_preview')
        succeeded = bool(result.get('succeeded'))
        binding = self._save_binding({**binding, 'last_find': None})
        summary = f'action {action} -> {"ok" if succeeded else "false"}'
        self._record_local_cli_command(
            'action',
            binding=binding,
            summary=summary,
            payload={
                'action': action,
                'succeeded': succeeded,
                'result_type': result_type,
                'result_preview': result_preview,
                'macro_warning': 'action is a dev/macro escape hatch; run hwpx where/export after stateful actions before trusting layout.',
            },
        )
        return {
            'ok': succeeded,
            'command': 'action',
            'mode': 'HAction.Run',
            'action': action,
            'result_type': result_type,
            'result_preview': result_preview,
            'warning': 'action is a dev/macro escape hatch; run hwpx where/export after stateful actions before trusting layout.',
            'summary': summary,
        }

    def _reduce_working_copy_dirty(
        self,
        *,
        prior_dirty: bool,
        semantic_ok: bool | None,
        delta_dirty: bool | None,
        may_have_mutated: bool,
        command_name: str,
        fresh_document_modified: bool | None,
        fresh_sequence_matches: bool,
        ordinary_save_confirmed: bool,
    ) -> tuple[bool, str]:
        """Reduce dirty state without allowing a stale false delta to clear it."""

        if semantic_ok is False and may_have_mutated:
            return True, 'semantic_failure_may_have_mutated'
        if semantic_ok is None and may_have_mutated:
            return True, 'semantic_uncertainty_may_have_mutated'
        if ordinary_save_confirmed and semantic_ok is True:
            return False, 'ordinary_save_confirmed'
        if fresh_sequence_matches and isinstance(fresh_document_modified, bool):
            if fresh_document_modified:
                return True, 'fresh_native_modified_state'
            if prior_dirty:
                return True, 'preserved_prior_dirty'
            return False, 'fresh_native_clean_state'
        if delta_dirty is True:
            return True, 'command_dirty_delta'
        if delta_dirty is False:
            return bool(prior_dirty), 'preserved_prior_dirty' if prior_dirty else 'command_clean_delta'
        return bool(prior_dirty), 'preserved_prior_dirty' if prior_dirty else 'unknown_preserved'

    def command_bundle(self, *, steps: list[dict[str, Any]], session_id: str | None = None) -> dict[str, Any]:
        cleaned_steps = self._validate_command_bundle_steps(steps)
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            # Bundle wrapper snapshots must not disturb live selection. Commands
            # that need rich nearby text/context should request it explicitly in
            # their own package; the wrapper is only envelope proof.
            before_location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
                include_nearby_context=False,
            )
            step_results: list[dict[str, Any]] = []
            warnings: list[str] = []
            artifacts: dict[str, Any] = {}
            dirty = False
            ok = True

            for index, step in enumerate(cleaned_steps, start=1):
                step_before = self._bundle_compact_snapshot(handle.hwp)
                try:
                    result, step_dirty, step_warnings = self._execute_command_bundle_step(handle, step, binding=binding)
                    dirty = dirty or bool(step_dirty)
                    if isinstance(result, dict) and result.get('artifact_kind') and result.get('artifact_path'):
                        artifacts[f"latest_{result.get('artifact_kind')}_path"] = str(result.get('artifact_path'))
                    status = {
                        'index': index,
                        'label': step.get('label'),
                        'op': step.get('op'),
                        'ok': True,
                        'dirty': bool(step_dirty),
                        'before': step_before,
                        'after': self._bundle_compact_snapshot(handle.hwp),
                        'result': result,
                        'warnings': step_warnings,
                    }
                    warnings.extend(step_warnings)
                except Exception as exc:
                    ok = False
                    mutation_may_have_persisted = bool(getattr(exc, 'mutation_may_have_persisted', False))
                    rollback = getattr(exc, 'rollback', {})
                    if not isinstance(rollback, dict):
                        rollback = {}
                    dirty = dirty or mutation_may_have_persisted
                    status = {
                        'index': index,
                        'label': step.get('label'),
                        'op': step.get('op'),
                        'ok': False,
                        'dirty': mutation_may_have_persisted,
                        'before': step_before,
                        'after': self._bundle_compact_snapshot(handle.hwp),
                        'error': f'{type(exc).__name__}: {exc}',
                        'mutation_may_have_persisted': mutation_may_have_persisted,
                        'rollback': rollback,
                        'mutation': {
                            'may_have_persisted': mutation_may_have_persisted,
                            'rollback': rollback,
                        },
                    }
                    step_results.append(status)
                    break
                step_results.append(status)

            after_location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
                include_nearby_context=False,
            )
            return {
                'ok': ok,
                'dirty': dirty,
                'before_location': before_location,
                'after_location': after_location,
                'steps': step_results,
                'warnings': warnings,
                'artifacts': artifacts,
            }

        prior_native_sequence = binding.get('native_command_sequence', 0)
        result = self._execute_live(
            binding=binding,
            command_name='command-bundle',
            task_label='local_cli.command_bundle',
            handler=_handler,
            timeout=120.0,
        )
        location = result.get('after_location') if isinstance(result.get('after_location'), dict) else {}
        command_evidence = result.get('_local_cli_command') if isinstance(result.get('_local_cli_command'), dict) else {}
        semantic_ok = command_evidence.get('semantic_ok') if isinstance(command_evidence.get('semantic_ok'), bool) else (
            result.get('semantic_ok') if isinstance(result.get('semantic_ok'), bool) else None
        )
        command_sequence = command_evidence.get('sequence')
        try:
            fresh_sequence_matches = isinstance(command_sequence, int) and command_sequence > int(prior_native_sequence)
        except (TypeError, ValueError):
            fresh_sequence_matches = False
        dirty, dirty_source = self._reduce_working_copy_dirty(
            prior_dirty=binding.get('working_copy_dirty') is True or binding.get('dirty') is True,
            semantic_ok=semantic_ok,
            delta_dirty=result.get('dirty') if isinstance(result.get('dirty'), bool) else None,
            may_have_mutated=result.get('may_have_mutated') is True,
            command_name='command-bundle',
            fresh_document_modified=location.get('document_is_modified') if isinstance(location.get('document_is_modified'), bool) else None,
            fresh_sequence_matches=fresh_sequence_matches,
            ordinary_save_confirmed=False,
        )
        artifacts = self._validated_artifact_projection(
            binding=binding,
            artifacts=result.get('artifacts') if isinstance(result.get('artifacts'), dict) else {},
        )
        binding = self._update_live_binding(
            binding,
            location=location,
            artifacts=artifacts,
            dirty=dirty,
            clear_last_find=dirty,
            clear_selection_cache=dirty,
        )
        if dirty:
            dirty_step_count = sum(1 for step in (result.get('steps') or []) if isinstance(step, dict) and step.get('dirty'))
            binding['pending_logical_undo_count'] = max(1, dirty_step_count)
            binding = self._save_binding(binding)
        summary = f"command-bundle {'succeeded' if result.get('ok') else 'stopped'}: {len(result.get('steps') or [])}/{len(cleaned_steps)} step(s)"
        self._record_local_cli_command(
            'command-bundle',
            binding=binding,
            summary=summary,
            payload={
                'ok': bool(result.get('ok')),
                'dirty': dirty,
                'dirty_source': dirty_source,
                'semantic_ok': semantic_ok,
                'may_have_mutated': result.get('may_have_mutated') is True,
                'step_count': len(cleaned_steps),
                'warnings': result.get('warnings') if isinstance(result.get('warnings'), list) else [],
                'artifacts': artifacts,
            },
        )
        return {
            'ok': bool(semantic_ok) if isinstance(semantic_ok, bool) else bool(result.get('ok')),
            'semantic_ok': semantic_ok,
            'command': 'command-bundle',
            'summary': summary,
            'dirty': dirty,
            'before': self._bundle_compact_location(result.get('before_location') if isinstance(result.get('before_location'), dict) else {}),
            'after': self._bundle_compact_location(location),
            'steps': self._public_bundle_steps(
                session_id=self._binding_session_id(binding),
                steps=result.get('steps'),
                binding=binding,
            ),
            'warnings': result.get('warnings') if isinstance(result.get('warnings'), list) else [],
            'artifacts': self._public_artifacts(
                session_id=self._binding_session_id(binding),
                artifacts=artifacts,
                binding=binding,
            ),
            **self._compact_state_payload(location=location),
        }

    # ------------------------------------------------------------------
    # Standalone targeted four-margin getter (document-read-only).
    #
    # The private method names below are a fixed responsibility map:
    #   cell_margins_get               public entry point / response shape
    #   _cell_margins_get_native       one ordered native observation walk
    #   _cell_margins_assert_document  strict live path/size/hash identity
    #   _cell_margins_document_generation  native-only text generation value
    #   _cell_margins_resolve_target   exact control/cell/anchor/page binding
    #   _cell_margins_restore_position failure-propagating navigation restore
    # No cached, default, XML, ambient-caret, first-table or suffix-path
    # fallback exists anywhere in this path, and no setter is reachable.
    # ------------------------------------------------------------------

    @staticmethod
    def _cell_margins_fail(code: str, message: str, details: dict[str, Any] | None = None) -> 'LocalCliCellMarginsGetError':
        return LocalCliCellMarginsGetError(code, message, details or {})

    def _cell_margins_assert_document(
        self,
        hwp: Any,
        working_copy_path: Path,
        *,
        size_bytes: int,
        sha256: str,
    ) -> None:
        """Assert the native document is the exact managed on-disk working copy."""

        doc_path = str(_safe_hwp_value(hwp, 'Path') or '').strip()
        if not doc_path:
            raise self._cell_margins_fail(
                'DOCUMENT_IDENTITY_MISMATCH',
                'The native document path is unavailable; refusing the read.',
            )
        if not self._cell_margins_native_path_names(doc_path, working_copy_path):
            raise self._cell_margins_fail(
                'DOCUMENT_IDENTITY_MISMATCH',
                'The live document is not the managed working copy for this session.',
            )
        custody: dict[str, Any] = {}
        self._verify_artifact_readback(
            self._cell_margins_custody_binding,
            working_copy_path,
            expected={'size_bytes': size_bytes, 'sha256': sha256},
            readback=custody,
        )

    @staticmethod
    def _cell_margins_native_path_names(native_path: str, managed_path: Path) -> bool:
        """Exact absolute-path equality with Windows normalization; never suffix matching."""

        def _normalize(value: str) -> str:
            text = str(value).strip().replace('/', '\\')
            parts = [part for part in text.split('\\') if part not in ('', '.')]
            # Keep the drive prefix; drop only dot components; casefold separators.
            return '\\'.join(part.casefold() for part in parts)

        native = _normalize(native_path)
        managed = _normalize(str(managed_path.resolve()))
        if not native or not managed:
            return False
        return native == managed

    def _cell_margins_document_generation(self, hwp: Any, *, session_id: str) -> str:
        """Fresh native-only text read; the find-generation producer without any open/disk fallback."""

        if hasattr(hwp, 'get_text_file'):
            text = hwp.get_text_file(format='UNICODE', option='')
        elif hasattr(hwp, 'GetTextFile'):
            text = hwp.GetTextFile('UNICODE', '')
        else:
            raise self._cell_margins_fail(
                'DOCUMENT_STATE_UNAVAILABLE',
                'Native text reading is unavailable on this runtime.',
            )
        records = load_plain_text_records(str(text or ''))
        if not records:
            raise self._cell_margins_fail(
                'DOCUMENT_STATE_UNAVAILABLE',
                'The native text observation is empty; an anchor-bearing read cannot proceed.',
            )
        live_text = '\n'.join(str(item.get('text') or '') for item in records)
        digest = hashlib.sha256(live_text.encode('utf-8')).hexdigest()
        return f'local-cli/live-document/v1:{session_id}:sha256:{digest}'

    def _cell_margins_resolve_target(
        self,
        hwp: Any,
        *,
        target: CellMarginsGetTarget,
    ) -> dict[str, Any]:
        """Resolve exactly one full target control and bind cell/page/anchor in one live observation."""

        head_ctrl = getattr(hwp, 'HeadCtrl', None)
        head_ctrl = head_ctrl() if callable(head_ctrl) else head_ctrl
        if head_ctrl is None:
            raise self._cell_margins_fail(
                'TARGET_IDENTITY_UNAVAILABLE',
                'The native control enumeration head is unavailable.',
            )
        try:
            controls, enumeration_mode = _enumerate_controls_headctrl(hwp, max_controls=target.max_controls + 1)
        except EditOperationError as exc:
            raise self._cell_margins_fail(
                'TARGET_ENUMERATION_INCOMPLETE',
                'The control inventory could not be completely enumerated.',
                {'reason': str(exc)},
            ) from exc
        if enumeration_mode != 'HeadCtrl->Next':
            raise self._cell_margins_fail(
                'TARGET_IDENTITY_UNAVAILABLE',
                'The control inventory fell back to an uncapped source; refusing the read.',
            )
        if len(controls) > target.max_controls:
            raise self._cell_margins_fail(
                'TARGET_ENUMERATION_INCOMPLETE',
                'The control inventory exceeds max_controls; completeness cannot be proven.',
            )

        expected_hash = 'sha256:' + (target.expected_hash[7:] if target.expected_hash.startswith('sha256:') else target.expected_hash)
        matching: list[tuple[Any, dict[str, Any]]] = []
        seen_locators: set[str] = set()
        for index, ctrl in enumerate(controls):
            item, _snapshot, _anchor_pos = self._bundle_control_proof_item(hwp, ctrl, index)
            locator = str(item.get('target_id') or '')
            if locator in seen_locators:
                raise self._cell_margins_fail(
                    'TARGET_ENUMERATION_INCOMPLETE',
                    'The control inventory repeated one locator; enumeration is not trustworthy.',
                )
            seen_locators.add(locator)
            if locator == target.target_id:
                matching.append((ctrl, item))
        if not matching:
            raise self._cell_margins_fail('TARGET_NOT_FOUND', 'No control matches the requested exact target_id.')
        if len(matching) > 1:
            raise self._cell_margins_fail('TARGET_AMBIGUOUS', 'More than one control matches the requested target_id.')

        target_ctrl, item = matching[0]
        ctrl_id = str(item.get('ctrl_id') or '')
        ctrl_inst_id = str(item.get('ctrl_inst_id') or '')
        if ctrl_id != 'tbl' or not ctrl_inst_id or ctrl_inst_id == 'no-inst':
            raise self._cell_margins_fail('TARGET_NOT_TABLE', 'The resolved target is not a proven table control.')
        if str(item.get('proof_hash') or '') != expected_hash:
            raise self._cell_margins_fail(
                'TARGET_HASH_MISMATCH',
                'The control inventory proof does not match the expected_hash.',
            )
        inventory_page = item.get('page')
        if isinstance(inventory_page, bool) or not isinstance(inventory_page, int) or inventory_page <= 0:
            raise self._cell_margins_fail(
                'PAGE_UNAVAILABLE',
                'The inventory cannot prove the table anchor page.',
            )
        if inventory_page != target.expected_page:
            raise self._cell_margins_fail(
                'PAGE_MISMATCH',
                'The table anchor page does not match expected_page.',
            )

        anchor_page = self._cell_margins_read_current_page(hwp)
        if anchor_page != target.expected_page:
            raise self._cell_margins_fail(
                'PAGE_MISMATCH',
                'The live table anchor page does not match expected_page.',
            )

        return {
            'target_ctrl': target_ctrl,
            'item': item,
            'ctrl_inst_id': ctrl_inst_id,
            'anchor_page': anchor_page,
        }

    @staticmethod
    def _cell_margins_read_current_page(hwp: Any) -> int | None:
        """Direct current_page probe (property or zero-argument method) only."""

        current_page = getattr(hwp, 'current_page', None)
        try:
            value = current_page() if callable(current_page) else current_page
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    def _cell_margins_restore_position(self, hwp: Any, original_pos: tuple[int, int, int]) -> None:
        """Low-level SetPos restoration; any failure propagates to the caller."""

        _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))

    def _cell_margins_get_native(
        self,
        hwp: Any,
        *,
        request: CellMarginsGetRequest,
        request_sha256: str,
        handle_session_id: str,
        working_copy_path: Path,
        working_copy_custody: dict[str, Any],
    ) -> dict[str, Any]:
        """One ordered native observation walk. See architecture spec section 4."""

        target = request.request

        # 3. Capture live original state; require no selection.
        try:
            original_pos = _get_pos(hwp)
            if len(original_pos) < 3:
                raise self._cell_margins_fail(
                    'DOCUMENT_STATE_UNAVAILABLE',
                    'The native cursor position is unavailable.',
                )
            original_pos = (int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
        except LocalCliCellMarginsGetError:
            raise
        except Exception as exc:
            raise self._cell_margins_fail(
                'DOCUMENT_STATE_UNAVAILABLE',
                'The native cursor state is unavailable.',
                {'reason': f'{type(exc).__name__}'},
            ) from exc

        def _selection_state() -> tuple[Any, Any]:
            try:
                selected = _get_selected_pos(hwp)
            except Exception:
                selected = None
            selection_mode = _get_selection_mode(hwp)
            return selected, selection_mode

        selected_before, selection_mode_before = _selection_state()
        if selected_before and selected_before[0]:
            raise self._cell_margins_fail(
                'ACTIVE_SELECTION_UNSUPPORTED',
                'A text or block selection is active; clear it before reading margins.',
            )
        try:
            mode_value = int(selection_mode_before)
        except (TypeError, ValueError):
            mode_value = None
        if selection_mode_before is None or mode_value is None:
            raise self._cell_margins_fail(
                'DOCUMENT_STATE_UNAVAILABLE',
                'The native selection state is unavailable.',
            )
        if mode_value != 0:
            raise self._cell_margins_fail(
                'ACTIVE_SELECTION_UNSUPPORTED',
                'The native selection mode is not a plain caret.',
            )

        def _read_is_modified() -> bool:
            value = _safe_hwp_value(hwp, 'IsModified')
            if isinstance(value, bool) or (isinstance(value, int) and not isinstance(value, bool)):
                return bool(value)
            raise self._cell_margins_fail(
                'DOCUMENT_STATE_UNAVAILABLE',
                'The native modification flag is unavailable.',
            )

        modified_before = _read_is_modified()

        navigation_attempted = False

        try:
            # 2/4. Assert document identity and fresh generation before reading.
            try:
                self._cell_margins_assert_document(
                    hwp,
                    working_copy_path,
                    size_bytes=int(working_copy_custody['size_bytes']),
                    sha256=str(working_copy_custody['sha256']),
                )
            except LocalCliServiceError:
                raise
            except LocalCliCellMarginsGetError:
                raise
            generation_before = self._cell_margins_document_generation(hwp, session_id=handle_session_id)
            if generation_before != target.expected_document_generation:
                raise self._cell_margins_fail(
                    'DOCUMENT_GENERATION_MISMATCH',
                    'The fresh native text generation does not match expected_document_generation.',
                )

            # 5. Exact target control resolution.
            resolved = self._cell_margins_resolve_target(hwp, target=target)
            target_ctrl = resolved['target_ctrl']
            ctrl_inst_id = resolved['ctrl_inst_id']

            # 6. Direct cell entry by exact position.
            navigation_attempted = True
            _set_pos(hwp, int(target.cell_pos[0]), int(target.cell_pos[1]), int(target.cell_pos[2]))
            cell_snapshot = _snapshot_cursor_context(hwp)
            if not self._cell_margins_cell_state_ok(hwp, cell_snapshot, target):
                raise self._cell_margins_fail(
                    'CELL_TARGET_MISMATCH',
                    'The caret did not land in the requested cell of the requested table.',
                )
            cell_page = self._cell_margins_read_current_page(hwp)
            if cell_page is None:
                raise self._cell_margins_fail('PAGE_UNAVAILABLE', 'The rendered cell page is unavailable.')
            if cell_page != target.expected_cell_page:
                raise self._cell_margins_fail(
                    'PAGE_MISMATCH',
                    'The rendered cell page does not match expected_cell_page.',
                )
            paragraph_text = self._cell_margins_current_paragraph(hwp)
            occurrences = paragraph_text.count(target.section_anchor)
            if occurrences != 1:
                raise self._cell_margins_fail(
                    'SECTION_ANCHOR_MISMATCH',
                    'The literal anchor must occur exactly once in the target cell paragraph.',
                    {'occurrences': occurrences},
                )

            # 7. One fresh native four-side observation through the existing reader.
            readback = self._bundle_native_cell_margin_readback(hwp, expected_cell_addr=list(target.cell_addr))
            if readback.get('available') is not True or readback.get('refresh_succeeded') is not True:
                raise self._cell_margins_fail(
                    'NATIVE_MARGIN_UNAVAILABLE',
                    'The fresh native four-side observation is unavailable.',
                    {'stage': 'native-refresh'},
                )
            margins = _normalize_cell_margin_readback(readback.get('value'))
            if margins is None:
                raise self._cell_margins_fail(
                    'NATIVE_MARGIN_UNAVAILABLE',
                    'The native four-side values failed strict validation.',
                    {'stage': 'value-validation'},
                )
            refresh_count = getattr(hwp, 'get_default_count', None)

            # 8. Immediate post-refresh identity and state reassertion.
            post_doc_path = str(_safe_hwp_value(hwp, 'Path') or '').strip()
            if not post_doc_path or not self._cell_margins_native_path_names(post_doc_path, working_copy_path):
                raise self._cell_margins_fail(
                    'DOCUMENT_CHANGED_DURING_READ',
                    'The live document identity changed during the read.',
                )
            post_snapshot = _snapshot_cursor_context(hwp)
            if not self._cell_margins_cell_state_ok(hwp, post_snapshot, target):
                raise self._cell_margins_fail(
                    'CELL_TARGET_MISMATCH',
                    'The cell identity changed during the read.',
                )
            post_page = self._cell_margins_read_current_page(hwp)
            if post_page != target.expected_cell_page:
                raise self._cell_margins_fail(
                    'PAGE_MISMATCH',
                    'The rendered cell page changed during the read.',
                )
            parent_summary = _safe_parent_ctrl_summary(hwp)
            if parent_summary is None or str(parent_summary.get('CtrlInstID') or '') != ctrl_inst_id:
                raise self._cell_margins_fail(
                    'CELL_TARGET_MISMATCH',
                    'The immediate parent table identity changed during the read.',
                )
            resolved_after = self._cell_margins_resolve_target(hwp, target=target)
            if str(resolved_after['item'].get('proof_hash') or '') != str(resolved['item'].get('proof_hash') or ''):
                raise self._cell_margins_fail(
                    'TARGET_CHANGED_DURING_READ',
                    'The target control proof changed during the read.',
                )
            generation_after = self._cell_margins_document_generation(hwp, session_id=handle_session_id)
            if generation_after != generation_before:
                raise self._cell_margins_fail(
                    'DOCUMENT_CHANGED_DURING_READ',
                    'The document text changed during the read.',
                )
        except LocalCliCellMarginsGetError as exc:
            # Cleanup path: confirm restoration and post-state; promote the
            # primary failure code per the error-precedence contract.
            if navigation_attempted:
                try:
                    self._cell_margins_restore_position(hwp, original_pos)
                except Exception:
                    exc.primary_code = 'NAVIGATION_RESTORE_FAILED'
                    exc.details['secondary_codes'] = self._cell_margins_bounded_codes(
                        exc.details.get('secondary_codes'), exc.code)
                else:
                    post_selected, post_mode = _selection_state()
                    try:
                        post_mode_value = int(post_mode)
                    except (TypeError, ValueError):
                        post_mode_value = None
                    if (post_selected and post_selected[0]) or post_mode_value != 0:
                        exc.primary_code = 'NAVIGATION_RESTORE_FAILED'
                        exc.details['secondary_codes'] = self._cell_margins_bounded_codes(
                            exc.details.get('secondary_codes'), exc.code)
            raise

        # 9. Confirmed restoration of the captured original position.
        try:
            self._cell_margins_restore_position(hwp, original_pos)
        except Exception as exc:
            raise self._cell_margins_fail(
                'NAVIGATION_RESTORE_FAILED',
                'The original caret position could not be restored; margin values are suppressed.',
                {'secondary_codes': []},
            ) from exc
        restored_selected, restored_mode = _selection_state()
        try:
            restored_mode_value = int(restored_mode)
        except (TypeError, ValueError):
            restored_mode_value = None
        navigation_restored = not (restored_selected and restored_selected[0]) and restored_mode_value == 0

        # 10. Post-read document state comparison.
        modified_after = _read_is_modified()
        post_path = str(_safe_hwp_value(hwp, 'Path') or '').strip()
        identity_stable = bool(post_path) and self._cell_margins_native_path_names(post_path, working_copy_path)
        try:
            self._cell_margins_assert_document(
                hwp,
                working_copy_path,
                size_bytes=int(working_copy_custody['size_bytes']),
                sha256=str(working_copy_custody['sha256']),
            )
        except LocalCliServiceError:
            identity_stable = False
        except LocalCliCellMarginsGetError:
            identity_stable = False

        document_state_unchanged = (
            identity_stable
            and modified_before == modified_after
            and navigation_restored
        )
        if not document_state_unchanged:
            raise self._cell_margins_fail(
                'DOCUMENT_CHANGED_DURING_READ'
                if (not identity_stable or modified_before != modified_after)
                else 'NAVIGATION_RESTORE_FAILED',
                'The post-read state could not be proven unchanged.',
            )

        # 11. Bounded public projection.
        return {
            'ok': True,
            'semantic_ok': True,
            'document_generation': generation_before,
            'margins': margins,
            'ctrl_inst_id': ctrl_inst_id,
            'cell_addr': list(target.cell_addr),
            'anchor_page': resolved['anchor_page'],
            'cell_page': target.expected_cell_page,
            'navigation_restored': True,
            'document_modified_before': modified_before,
            'document_modified_after': modified_after,
            'refresh_count': refresh_count,
            'request_sha256': request_sha256,
            'working_copy_custody': dict(working_copy_custody),
        }

    @staticmethod
    def _cell_margins_bounded_codes(existing: Any, code: str) -> list[str]:
        codes = [str(item) for item in existing] if isinstance(existing, list) else []
        if code not in codes:
            codes.append(code)
        return codes[:4]

    @staticmethod
    def _cell_margins_cell_state_ok(hwp: Any, snapshot: Mapping[str, Any], target: CellMarginsGetTarget) -> bool:
        if not isinstance(snapshot, Mapping):
            return False
        if snapshot.get('is_cell') is not True and snapshot.get('is_cell') is not False:
            pass
        if snapshot.get('is_cell') is not True:
            return False
        if snapshot.get('has_selection'):
            return False
        try:
            mode_value = int(snapshot.get('selection_mode'))
        except (TypeError, ValueError):
            return False
        if mode_value != 0:
            return False
        observed_addr = _normalize_cell_addr_value(snapshot.get('cell_addr'))
        if observed_addr != list(target.cell_addr):
            return False
        parent_summary = _safe_parent_ctrl_summary(hwp)
        if parent_summary is None:
            return False
        return True

    @staticmethod
    def _cell_margins_current_paragraph(hwp: Any) -> str:
        """Read the current paragraph through the existing paragraph reader."""

        try:
            text = _get_current_paragraph_text_at_cursor(hwp)
        except Exception as exc:
            raise LocalCliCellMarginsGetError(
                'DOCUMENT_STATE_UNAVAILABLE',
                'The current paragraph could not be read.',
                {'reason': f'{type(exc).__name__}'},
            ) from exc
        return str(text or '')

    def cell_margins_get(self, *, session_id: str | None = None, request: CellMarginsGetRequest | None = None) -> dict[str, Any]:
        """POST /local-cli/cell-margins-get — one exact-target native margin observation.

        Admission, live-state and identity checks run again inside the queued
        handler, not only in the adapter, so the observation is bound to the
        state at execution time. The response is a bounded public object; no
        internal handler dictionary escapes.
        """

        if request is None:
            raise LocalCliServiceError('cell-margins-get requires the shared request model.', status_code=400)
        target = request.request
        binding = self._load_active_binding(session_id=session_id or target.document_id)
        resolved_session_id = self._binding_session_id(binding)
        if resolved_session_id != request.session_id or target.document_id != request.session_id:
            raise LocalCliServiceError('Session and document identity do not match.', status_code=409)
        if self._binding_has_pending_reconciliation(binding):
            raise LocalCliServiceError(
                'A native local CLI command is awaiting reconciliation; the getter cannot run.',
                status_code=409,
            )
        working_copy_path = self._working_copy_path(binding)

        custody: dict[str, Any] = {}
        self._verify_artifact_readback(binding, working_copy_path, readback=custody)
        expected = binding.get('artifact_custody') if isinstance(binding.get('artifact_custody'), dict) else {}
        working_copy_custody = {
            'size_bytes': custody.get('size_bytes'),
            'sha256': 'sha256:' + str(custody.get('sha256')),
            'basis': 'managed-on-disk-copy-not-live-format-state',
        }
        if not isinstance(working_copy_custody['size_bytes'], int) or working_copy_custody['size_bytes'] <= 0:
            raise LocalCliServiceError('Managed working-copy custody could not be established.', status_code=409)
        del expected

        binding_dirty_before = binding.get('working_copy_dirty')
        pending_logical_undo_count = binding.get('pending_logical_undo_count')

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            if handle.session_id != request.session_id or handle.session_id != target.document_id:
                raise self._cell_margins_fail(
                    'DOCUMENT_IDENTITY_MISMATCH',
                    'The live runtime handle does not match the requested session identity.',
                )
            if Path(str(handle.working_copy_path)) != working_copy_path:
                raise self._cell_margins_fail(
                    'DOCUMENT_IDENTITY_MISMATCH',
                    'The live runtime handle does not reference the managed working copy.',
                )
            self._cell_margins_custody_binding.clear()
            self._cell_margins_custody_binding.update({
                'session_root_path': str(handle.session_root),
                'session_root_identity': self._managed_path_identity(handle.session_root),
            })
            if not isinstance(self._cell_margins_custody_binding['session_root_identity'], dict):
                raise self._cell_margins_fail(
                    'DOCUMENT_IDENTITY_MISMATCH',
                    'The managed session root identity is unavailable.',
                )
            try:
                native = self._cell_margins_get_native(
                    handle.hwp,
                    request=request,
                    request_sha256=request_sha256,
                    handle_session_id=handle.session_id,
                    working_copy_path=working_copy_path,
                    working_copy_custody=working_copy_custody,
                )
            except LocalCliCellMarginsGetError as exc:
                return {
                    'failed': True,
                    'code': exc.primary_code,
                    'message': str(exc),
                    'details': exc.details,
                }
            location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
                include_nearby_context=False,
                include_document_snapshot=False,
            )
            native['location'] = location
            return native

        request_sha256 = canonical_cell_margins_request_sha256(request)
        result = self._execute_live(
            binding=binding,
            command_name='cell_margins_get',
            task_label='local_cli.cell_margins_get',
            handler=_handler,
        )
        command_evidence = result.get('_local_cli_command') if isinstance(result.get('_local_cli_command'), dict) else {}
        semantic_ok = command_evidence.get('semantic_ok')
        command_state = command_evidence.get('state')
        command_id = command_evidence.get('command_id')
        sequence = command_evidence.get('sequence')
        if result.get('failed') is True:
            return self._cell_margins_failure_response(
                session_id=request.session_id,
                code=str(result.get('code') or 'DOCUMENT_STATE_UNAVAILABLE'),
                message=str(result.get('message') or 'The targeted read failed.'),
                details=result.get('details') if isinstance(result.get('details'), dict) else {},
                command={'command_id': command_id, 'sequence': sequence, 'state': command_state},
                dirty=None,
                may_have_mutated=False,
            )
        if semantic_ok is not True or command_state != 'succeeded':
            return self._cell_margins_failure_response(
                session_id=request.session_id,
                code='DOCUMENT_STATE_UNAVAILABLE',
                message='The runtime command did not report a succeeded semantic state.',
                details={'stage': 'command-status'},
                command={'command_id': command_id, 'sequence': sequence, 'state': command_state},
                dirty=None,
                may_have_mutated=True,
            )
        if not isinstance(command_id, str) or not command_id or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            return self._cell_margins_failure_response(
                session_id=request.session_id,
                code='DOCUMENT_STATE_UNAVAILABLE',
                message='The runtime command identity is incomplete.',
                details={'stage': 'command-identity'},
                command={'command_id': command_id, 'sequence': sequence, 'state': command_state},
                dirty=None,
                may_have_mutated=True,
            )

        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(
            binding,
            location=location,
            dirty=bool(binding_dirty_before) if isinstance(binding_dirty_before, bool) else None,
        )
        binding['working_copy_dirty'] = binding_dirty_before if isinstance(binding_dirty_before, bool) else bool(binding.get('working_copy_dirty'))
        if isinstance(pending_logical_undo_count, int):
            binding['pending_logical_undo_count'] = pending_logical_undo_count
        self._save_binding(binding)
        self._record_local_cli_command(
            'cell_margins_get',
            binding=binding,
            summary=f'observed four-side margins for {target.target_id}',
            payload={
                'ok': True,
                'dirty': False,
                'semantic_ok': True,
                'may_have_mutated': False,
                'read_only': True,
            },
        )
        modified_before = bool(result.get('document_modified_before'))
        modified_after = bool(result.get('document_modified_after'))
        margins = result.get('margins') or {}
        observed_at = utc_now_iso()
        return {
            'schema_version': 'local-cli/cell-margins-get/v1',
            'operation': 'cell_margins_get',
            'ok': True,
            'semantic_ok': True,
            'session_id': request.session_id,
            'document_id': request.session_id,
            'read_only': True,
            'dirty': False,
            'may_have_mutated': False,
            'mutation_may_have_persisted': False,
            'request_sha256': result.get('request_sha256'),
            'document_generation': result.get('document_generation'),
            'working_copy_file': result.get('working_copy_custody'),
            'target': {
                'target_id': target.target_id,
                'proof_hash': 'sha256:' + target.expected_hash[7:],
                'ctrl_inst_id': result.get('ctrl_inst_id'),
                'anchor_page': result.get('anchor_page'),
                'cell_page': result.get('cell_page'),
                'cell_pos': list(target.cell_pos),
                'cell_addr': list(target.cell_addr),
                'page_from': target.page_from,
                'page_to': target.page_to,
                'section_anchor_sha256': 'sha256:' + hashlib.sha256(target.section_anchor.encode('utf-8')).hexdigest(),
                'section_binding': 'literal-in-target-cell-paragraph',
            },
            'unit': 'hwpunit',
            'units_per_inch': 7200,
            'side_order': ['left', 'right', 'top', 'bottom'],
            'margins_hu': {'left': margins.get('left'), 'right': margins.get('right'), 'top': margins.get('top'), 'bottom': margins.get('bottom')},
            'provenance': {
                'source': 'HParameterSet.HShapeObject.ShapeTableCell.Margin*',
                'refresh_action': 'TablePropertyDialog',
                'refresh_method': 'HAction.GetDefault',
                'refresh_succeeded': True,
                'cache_used': False,
                'observed_at_utc': observed_at,
                'target_verified_before': True,
                'target_verified_after': True,
            },
            'state': {
                'document_modified_before': modified_before,
                'document_modified_after': modified_after,
                'binding_dirty_before': bool(binding_dirty_before) if isinstance(binding_dirty_before, bool) else bool(binding.get('working_copy_dirty')),
                'binding_dirty_after': bool(binding.get('working_copy_dirty')),
                'document_state_unchanged': True,
                'navigation_restored': True,
                'selection_cache_invalidated': False,
                'mutation_attempted': False,
            },
            'error': None,
            'command': {'command_id': command_id, 'sequence': sequence, 'state': command_state},
        }

    def _cell_margins_failure_response(
        self,
        *,
        session_id: str,
        code: str,
        message: str,
        details: dict[str, Any],
        command: dict[str, Any] | None,
        dirty: bool | None,
        may_have_mutated: bool,
    ) -> dict[str, Any]:
        """One bounded failure shape shared by every handled getter failure."""

        bounded_details = dict(details)
        bounded_details.setdefault('mutation_attempted', False)
        command_payload = None
        if isinstance(command, dict):
            command_payload = {
                'command_id': command.get('command_id') if isinstance(command.get('command_id'), str) else None,
                'sequence': command.get('sequence') if isinstance(command.get('sequence'), int) and not isinstance(command.get('sequence'), bool) else None,
                'state': str(command.get('state') or '') or None,
            }
        return {
            'schema_version': 'local-cli/cell-margins-get/v1',
            'operation': 'cell_margins_get',
            'ok': False,
            'semantic_ok': False,
            'session_id': session_id,
            'document_id': session_id,
            'read_only': True,
            'dirty': dirty,
            'may_have_mutated': bool(may_have_mutated),
            'mutation_may_have_persisted': bool(may_have_mutated),
            'observation': None,
            'error': {'code': code, 'message': message, 'details': bounded_details},
            'command': command_payload,
        }


    def screenshot(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        resolved_session_id = self._binding_session_id(binding)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            result = capture_screenshot_artifact(
                session_id=handle.session_id,
                session_root=handle.session_root,
                hwp=handle.hwp,
                log_path=handle.log_path,
            )
            return {
                'artifact_path': str(result['artifact_path']),
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='screenshot', task_label='local_cli.screenshot', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        artifact_path = str(result.get('artifact_path') or '')
        artifacts = self._validated_artifact_projection(
            binding=binding,
            artifacts={'latest_screenshot_path': artifact_path},
        )
        binding = self._update_live_binding(binding, location=location, artifacts=artifacts)
        self._record_local_cli_command('screenshot', binding=binding, summary='captured editor screenshot', payload={'artifact_path': artifact_path})
        return {
            'ok': True,
            'session_id': resolved_session_id,
            'filename': self._artifact_name(kind='screenshot', source_filename=str(binding.get('source_filename') or 'document.hwpx')),
            'download_path': self._artifact_download_path(session_id=resolved_session_id, kind='screenshot'),
        }

    def save(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        working_copy_path = self._working_copy_path(binding)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            save_document(handle.hwp)
            if working_copy_path.is_symlink() or not working_copy_path.is_file():
                raise LocalCliRuntimeError('Native save returned without a regular working-copy file.')
            try:
                working_copy_size = working_copy_path.stat().st_size
            except OSError as exc:
                raise LocalCliRuntimeError('Saved working-copy readback failed.') from exc
            if working_copy_size <= 0:
                raise LocalCliRuntimeError('Native save produced an empty working copy.')
            working_copy_custody = {}
            self._verify_artifact_readback(binding, working_copy_path, readback=working_copy_custody)
            location = snapshot_live_location(
                hwp=handle.hwp,
                source_filename=handle.source_filename,
                working_copy_id=handle.session_id,
            )
            return {
                'location': location,
                'ordinary_save_confirmed': True,
                'working_copy_size_bytes': working_copy_size,
                'working_copy_custody': working_copy_custody,
            }

        result = self._execute_live(binding=binding, command_name='save', task_label='local_cli.save', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        command_evidence = result.get('_local_cli_command') if isinstance(result.get('_local_cli_command'), dict) else {}
        semantic_ok = command_evidence.get('semantic_ok') if isinstance(command_evidence.get('semantic_ok'), bool) else True
        dirty, dirty_source = self._reduce_working_copy_dirty(
            prior_dirty=binding.get('working_copy_dirty') is True or binding.get('dirty') is True,
            semantic_ok=semantic_ok,
            delta_dirty=False,
            may_have_mutated=result.get('may_have_mutated') is True,
            command_name='save',
            fresh_document_modified=location.get('document_is_modified') if isinstance(location.get('document_is_modified'), bool) else None,
            fresh_sequence_matches=True,
            ordinary_save_confirmed=result.get('ordinary_save_confirmed') is True,
        )
        working_copy_custody = result.get('working_copy_custody')
        if isinstance(working_copy_custody, dict):
            custody_map = binding.get('artifact_custody') if isinstance(binding.get('artifact_custody'), dict) else {}
            custody_map['working-copy'] = working_copy_custody
            binding['artifact_custody'] = custody_map
        binding = self._update_live_binding(
            binding,
            location=location,
            dirty=dirty,
            artifacts={'latest_working_copy_path': str(working_copy_path)},
        )
        self._record_local_cli_command(
            'save',
            binding=binding,
            summary='saved active working copy',
            payload={
                'working_copy_path': str(working_copy_path),
                'dirty': dirty,
                'dirty_source': dirty_source,
                'semantic_ok': semantic_ok,
            },
        )
        resolved_session_id = self._binding_session_id(binding)
        return {
            'ok': True,
            'session_id': resolved_session_id,
            'filename': self._artifact_name(kind='working-copy', source_filename=str(binding.get('source_filename') or 'document.hwpx')),
            'download_path': self._artifact_download_path(session_id=resolved_session_id, kind='working-copy'),
        }

    def export(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        resolved_session_id = self._binding_session_id(binding)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            artifact_path = export_document_pdf(
                session_root=handle.session_root,
                source_filename=handle.source_filename,
                hwp=handle.hwp,
                log_path=handle.log_path,
            )
            return {
                'artifact_path': str(artifact_path),
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='export', task_label='local_cli.export', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        artifact_path = str(result.get('artifact_path') or '')
        artifacts = self._validated_artifact_projection(
            binding=binding,
            artifacts={'latest_export_path': artifact_path},
        )
        binding = self._update_live_binding(binding, location=location, artifacts=artifacts)
        self._record_local_cli_command('export', binding=binding, summary='exported live document to PDF', payload={'artifact_path': artifact_path})
        return {
            'ok': True,
            'session_id': resolved_session_id,
            'filename': self._artifact_name(kind='export', source_filename=str(binding.get('source_filename') or 'document.hwpx')),
            'download_path': self._artifact_download_path(session_id=resolved_session_id, kind='export'),
        }

    def where(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            return {
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='where', task_label='local_cli.where', handler=_handler)
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location)
        self._record_local_cli_command('where', binding=binding, summary='reported current live cursor location', payload={'cursor': location.get('cursor')})
        return {
            'ok': True,
            'active_document': location.get('document_name') or binding.get('source_filename'),
            'document_path': location.get('document_path'),
            'working_copy_id': self._binding_session_id(binding),
            'cursor': location.get('cursor') or {},
            'cursor_summary': location.get('cursor_summary') or 'unknown',
            'selection_summary': location.get('selection_summary') or 'none',
            'current_paragraph_preview': location.get('current_paragraph_preview'),
            'document_is_modified': location.get('document_is_modified'),
            'caret_in_table_cell': location.get('caret_in_table_cell'),
            'page_count': location.get('page_count'),
            'selection_mode': location.get('selection_mode'),
            'current_selected_ctrl': location.get('current_selected_ctrl'),
            'parent_ctrl': location.get('parent_ctrl'),
        }

    def undo(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)
        undo_count_raw = binding.get('pending_logical_undo_count')
        undo_count = int(undo_count_raw) if isinstance(undo_count_raw, int) and undo_count_raw > 1 else 1

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            for _ in range(undo_count):
                self._run_single_action(
                    handle.hwp,
                    actions=('Undo',),
                    methods=('Undo',),
                    error_message='pyhwpx undo is unavailable on this machine',
                )
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='undo', task_label='local_cli.undo', handler=_handler)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        binding['pending_logical_undo_count'] = None
        binding = self._save_binding(binding)
        self._record_local_cli_command('undo', binding=binding, summary=f'applied undo to the live document ({undo_count} native step(s))', payload={'cursor': snapshot.get('pos'), 'native_undo_steps': undo_count})
        return {
            'ok': True,
            'summary': 'undo applied',
            'native_undo_steps': undo_count,
            'caret_pos': snapshot.get('pos'),
            'context': context,
        }

    def redo(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._load_active_binding(session_id=session_id)

        def _handler(handle: LocalCliRuntimeHandle) -> dict[str, Any]:
            self._run_single_action(
                handle.hwp,
                actions=('Redo',),
                methods=('Redo',),
                error_message='pyhwpx redo is unavailable on this machine',
            )
            snapshot = _snapshot_cursor_context(handle.hwp)
            context = _capture_nearby_text_context(handle.hwp)
            return {
                'snapshot': snapshot,
                'context': context,
                'location': snapshot_live_location(
                    hwp=handle.hwp,
                    source_filename=handle.source_filename,
                    working_copy_id=handle.session_id,
                ),
            }

        result = self._execute_live(binding=binding, command_name='redo', task_label='local_cli.redo', handler=_handler)
        snapshot = result.get('snapshot') if isinstance(result.get('snapshot'), dict) else {}
        context = result.get('context') if isinstance(result.get('context'), dict) else {}
        location = result.get('location') if isinstance(result.get('location'), dict) else {}
        binding = self._update_live_binding(binding, location=location, dirty=True, clear_last_find=True, clear_selection_cache=True)
        self._record_local_cli_command('redo', binding=binding, summary='applied redo to the live document', payload={'cursor': snapshot.get('pos')})
        return {
            'ok': True,
            'summary': 'redo applied',
            'caret_pos': snapshot.get('pos'),
            'context': context,
        }

    def artifact(self, *, kind: str, session_id: str | None = None) -> tuple[Path, str]:
        binding, path = self._resolve_artifact(kind=kind, session_id=session_id)
        return path, self._artifact_name(kind=kind, source_filename=str(binding.get('source_filename') or 'document.hwpx'))

    def close(self, *, session_id: str | None = None) -> dict[str, Any]:
        binding = self._read_binding(session_id=session_id)
        if not isinstance(binding, dict):
            if session_id:
                self._mark_session_closed(session_id)
            self._clear_binding(session_id=session_id)
            return {'ok': True}

        resolved_session_id = self._binding_session_id(binding)
        if self._binding_has_pending_reconciliation(binding):
            pending = binding.get('pending_command') if isinstance(binding.get('pending_command'), dict) else {}
            raise LocalCliServiceError(
                'Cannot close a local CLI session before its native command is reconciled: '
                f"{str(pending.get('command_id') or '').strip()}",
                status_code=409,
            )
        # Close admission is a persistence boundary too.  The runtime rejects
        # new work as soon as close begins; mark the tombstone only after
        # native close succeeds so a close timeout remains reconcilable.
        try:
            self.runtime_manager.close_session(resolved_session_id)
        except LocalCliRuntimeTimeoutError as exc:
            try:
                command_status = self.runtime_manager.command_status(resolved_session_id, exc.command_id)
            except Exception:
                command_status = {'command_id': exc.command_id, 'state': exc.command_state}
            try:
                current_sequence = self._parse_binding_generation(binding.get('native_command_sequence', 0))
            except LocalCliServiceError:
                current_sequence = 0
            try:
                raw_sequence = command_status.get('sequence', current_sequence)
                if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int) or raw_sequence < current_sequence:
                    raise ValueError('invalid or stale native command sequence')
                command_sequence = raw_sequence
            except (TypeError, ValueError) as exc:
                raise LocalCliServiceError('Local CLI close reconciliation sequence is invalid.', status_code=409) from exc
            binding['_expected_command_generation'] = binding.get('command_generation', 0)
            binding['_expected_native_command_sequence'] = current_sequence
            binding['native_command_sequence'] = command_sequence
            binding['pending_command'] = {
                'command_id': exc.command_id,
                'command': 'close',
                'sequence': command_sequence,
                'state': command_status.get('state', exc.command_state),
                'timed_out_at': utc_now_iso(),
            }
            binding['document_session_state'] = 'timed_out_pending_reconciliation'
            binding['live_session_bound'] = True
            self._save_binding(binding)
            try:
                self.interactive_sessions.record_command(
                    'close',
                    session_id=resolved_session_id,
                    state='pending',
                    summary='close timed out; awaiting native reconciliation',
                    payload={'command_id': exc.command_id, 'sequence': command_sequence},
                    metadata={'local_cli_v1': {'reconciliation_pending': True}},
                    live_runtime={
                        'reconciliation_pending': True,
                        'pending_command': dict(binding['pending_command']),
                    },
                )
            except Exception:
                pass
            raise LocalCliServiceError(
                f'{exc} status={command_status.get("state", exc.command_state)} '
                f'command_id={exc.command_id}; run command-reconcile before retrying.',
                status_code=504,
            ) from exc
        except LocalCliRuntimeError as exc:
            raise LocalCliServiceError('Local CLI native close failed.', status_code=500) from exc

        if binding.get('session_root_path'):
            try:
                cleanup_result = self._cleanup_managed_session_root(binding)
            except LocalCliServiceError:
                # Keep the binding as an operator-visible ownership record
                # when managed cleanup cannot prove the exact root was removed.
                binding['cleanup_pending'] = True
                binding['live_session_bound'] = False
                binding['document_session_state'] = 'closed_cleanup_pending'
                binding['updated_at'] = utc_now_iso()
                try:
                    self._save_binding(binding)
                except Exception:
                    pass
                raise
        else:
            # Bindings created before server-managed root custody was added do
            # not identify a removable directory; never guess one.
            cleanup_result = {
                'removed': False,
                'verified': True,
                'reason': 'no server-managed session root was recorded',
            }
        self._record_session_close(
            session_id=resolved_session_id,
            summary='Local CLI session closed.',
            outcome='closed',
        )
        self._clear_binding(session_id=resolved_session_id, force=True)
        self._mark_session_closed(resolved_session_id)
        public_cleanup = dict(cleanup_result) if isinstance(cleanup_result, dict) else {}
        public_cleanup.pop('path', None)
        return {'ok': True, 'cleanup': public_cleanup}



def as_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LocalCliServiceError):
        return HTTPException(status_code=exc.status_code, detail=exc.message)
    if isinstance(exc, LocalCliRuntimeError):
        return HTTPException(status_code=500, detail='Local CLI native operation failed.')
    return HTTPException(status_code=500, detail='Local CLI request failed.')
