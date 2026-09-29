from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import stat
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import HTTPException, UploadFile

from app.atomic_json import atomic_write_json, path_lock, read_json_object
from app.edit_ops import (
    EditOperationError,
    _build_find_candidates,
    _capture_nearby_text_context,
    _capture_selected_text_snapshot,
    _clear_current_cell_text,
    _delete_selection,
    _get_pos,
    _get_current_paragraph_text_at_cursor,
    _get_selected_text,
    _move_after_selection,
    _move_doc_begin,
    _normalize_visible_text,
    _preview_text,
    _resolve_current_table_cell_for_replacement,
    _run_table_cell_action,
    _select_text,
    _select_current_cell_contents,
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
from app.local_cli_service_support import (
    T,
    LocalCliArtifactDownload,
    _native_type_action_count,
    _IMAGE_ALLOWED_SUFFIXES,
    _build_live_heading_candidates,
    _live_candidate_is_query_anchor,
    _selected_text_contains_probe_relaxed,
    _MACRO_MAX_PATH_SEGMENTS,
    _MACRO_MAX_ARGS,
    _MACRO_MAX_KWARGS,
    _MACRO_MAX_JSON_DEPTH,
    _MACRO_MAX_STRING_CHARS,
    _MACRO_PREVIEW_STRING_CHARS,
    _MACRO_PREVIEW_ITEMS,
    _BUNDLE_MAX_STEPS,
    _BUNDLE_ALLOWED_OPS,
    _BUNDLE_SAFE_HACTION_NAMES,
    _BUNDLE_SAFE_PYHWPX_CALLS,
    _clean_asset_filename,
    LocalCliServiceError,
)
# Re-exported for existing importers of app.local_cli_service.
from app.local_cli_service_support import (  # noqa: F401
    LocalCliCellMarginsGetError,
    LocalCliMutationError,
    _require_observed_cell_format_mutation,
)
from app.local_cli_bundle_controls import LocalCliBundleControlsMixin
from app.local_cli_bundle_paragraphs import LocalCliBundleParagraphsMixin
from app.local_cli_cell_margins import LocalCliCellMarginsMixin
from app.local_cli_layout import LocalCliLayoutMixin
from app.local_cli_object_insert import LocalCliObjectInsertMixin


class LocalCliService(
    LocalCliBundleControlsMixin,
    LocalCliBundleParagraphsMixin,
    LocalCliCellMarginsMixin,
    LocalCliObjectInsertMixin,
    LocalCliLayoutMixin,
):
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
            'object_insert_exact': {
                'op',
                'operation',
                'label',
                'kind',
                'expected_pos',
                'confirm_mutation',
                'text',
                'url',
                'display_text',
                'name',
                'script',
                'width_mm',
                'height_mm',
                'treat_as_char',
                'apply_to',
            },
            'layout_exact': {
                'op',
                'operation',
                'label',
                'kind',
                'expected_pos',
                'paper_width_mm',
                'paper_height_mm',
                'landscape',
                'margin_top_mm',
                'margin_bottom_mm',
                'margin_left_mm',
                'margin_right_mm',
                'header_len_mm',
                'footer_len_mm',
                'gutter_len_mm',
                'count',
                'gap_mm',
                'same_width',
                'apply_to',
                'section_index',
                'marker_text',
                'expected_before',
                'confirm_layout',
            },
            'layout_inspect': {'op', 'operation', 'label'},
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
            location = self._bundle_where_location(handle.hwp, source_filename=handle.source_filename, working_copy_id=handle.session_id)
            return {'location': self._bundle_compact_location(location)}, False, warnings

        raise LocalCliRuntimeError(f'Unsupported command-bundle op: {op}')

    def _bundle_where_location(self, hwp: Any, *, source_filename: str, working_copy_id: str) -> dict[str, Any]:
        """Read-only location for a bundle ``where`` step that never disturbs a live selection.

        The nearby-paragraph capture moves the caret to read neighbouring
        paragraphs and restores only the caret, which drops a selection that a
        later step (hyperlink, memo) depends on; the compact bundle location
        does not report that preview, so it is skipped here. The selection
        range is re-read afterwards and any change is refused.
        """
        before = _snapshot_cursor_context(hwp)
        location = snapshot_live_location(
            hwp=hwp,
            source_filename=source_filename,
            working_copy_id=working_copy_id,
            include_nearby_context=False,
        )
        after = _snapshot_cursor_context(hwp)
        for key in ('pos', 'selected_pos', 'has_selection'):
            if before.get(key) != after.get(key):
                raise LocalCliRuntimeError(f'command-bundle where changed the live {key} ({before.get(key)!r} -> {after.get(key)!r}); refusing to continue')
        return location

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
