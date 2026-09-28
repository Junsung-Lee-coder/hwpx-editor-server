"""Read-only targeted four-side cell-margin getter for LocalCliService."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping


from app.edit_ops import (
    EditOperationError,
    _enumerate_controls_headctrl,
    _get_pos,
    _get_current_paragraph_text_at_cursor,
    _get_selected_pos,
    _get_selection_mode,
    _set_pos,
    _snapshot_cursor_context,
)
from app.local_cli_document import (
    load_plain_text_records,
)
from app.local_cli_runtime import (
    LocalCliRuntimeHandle,
    snapshot_live_location,
)
from app.models import CellMarginsGetRequest, CellMarginsGetTarget, canonical_cell_margins_request_sha256
from app.readiness import (
    utc_now_iso,
)
from app.local_cli_service_support import (
    _safe_hwp_value,
    _safe_parent_ctrl_summary,
    _normalize_cell_margin_readback,
    _normalize_cell_addr_value,
    LocalCliServiceError,
    LocalCliCellMarginsGetError,
)


class LocalCliCellMarginsMixin:
    """Read-only targeted four-side cell-margin getter for LocalCliService."""

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
