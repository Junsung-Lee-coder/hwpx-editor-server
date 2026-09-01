#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Keep this smoke dependency-light: the local test host may not have the full
# FastAPI/Pydantic runtime installed. Stub only the imports needed to load the
# LocalCliService helper under test; the helper itself still uses real edit_ops.
fastapi_stub = types.ModuleType('fastapi')
fastapi_stub.HTTPException = type('HTTPException', (Exception,), {})
fastapi_stub.UploadFile = type('UploadFile', (), {})
sys.modules.setdefault('fastapi', fastapi_stub)

runtime_stub = types.ModuleType('app.local_cli_runtime')
LocalCliRuntimeError = type('LocalCliRuntimeError', (RuntimeError,), {})
runtime_stub.LocalCliRuntimeError = LocalCliRuntimeError
# The static helper never exercises timeout construction; alias the
# dependency-light error class so the service import remains complete.
setattr(runtime_stub, 'LocalCliRuntimeTimeoutError', LocalCliRuntimeError)
runtime_stub.LocalCliRuntimeHandle = type('LocalCliRuntimeHandle', (), {})
for name in (
    'apply_char_style',
    'capture_screenshot_artifact',
    'create_table_at_cursor',
    'ensure_session_layout',
    'export_document_pdf',
    'get_local_cli_runtime_manager',
    'read_command_journal',
    'insert_multiline_text_at_caret_native',
    'insert_text_at_caret',
    'insert_text_at_caret_native',
    'insert_numbered_list_at_cursor',
    'save_document',
    'snapshot_live_location',
):
    setattr(runtime_stub, name, lambda *args, **kwargs: None)
sys.modules.setdefault('app.local_cli_runtime', runtime_stub)

readiness_stub = types.ModuleType('app.readiness')
readiness_stub.build_plain_readiness_failure = lambda *args, **kwargs: None
readiness_stub.load_runtime_readiness_snapshot = lambda *args, **kwargs: {}
setattr(readiness_stub, 'readiness_matches_current_worker', lambda *args, **kwargs: True)
setattr(readiness_stub, 'resolve_candidate_generation', lambda *args, **kwargs: 'smoke-candidate-generation')
readiness_stub.utc_now_iso = lambda: '2026-04-27T00:00:00Z'
sys.modules.setdefault('app.readiness', readiness_stub)

worker_stub = types.ModuleType('app.worker')
worker_stub.save_hwp_as = lambda *args, **kwargs: None
sys.modules.setdefault('app.worker', worker_stub)

from app.local_cli_service import LocalCliService  # noqa: E402


class FakeHwp:
    def __init__(
        self,
        *,
        collapse_on_read: bool = False,
        text: str = '증빙텍스트',
        selected: tuple[object, ...] | None = None,
    ) -> None:
        self.collapse_on_read = collapse_on_read
        self.text = text
        self.selected = selected if selected is not None else (True, 0, 0, 1, 0, 0, 6)
        self.pos = (0, 0, 1)
        self.restores: list[tuple[object, ...]] = []

    def get_pos(self):
        return self.pos

    def get_selected_pos(self):
        return self.selected

    def get_selected_text(self, *, keep_select: bool = False):
        if self.collapse_on_read or not keep_select:
            self.selected = (False, 0, 0, 1, 0, 0, 1)
        return self.text

    def select_text(self, selected_range):
        self.selected = tuple(selected_range)
        self.restores.append(tuple(selected_range))
        return True


class BrokenRestoreHwp(FakeHwp):
    def select_text(self, selected_range):
        self.restores.append(tuple(selected_range))
        self.selected = (False, 0, 0, 1, 0, 0, 1)
        return False


def service() -> LocalCliService:
    return LocalCliService.__new__(LocalCliService)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    svc = service()
    selection_cache = {
        'selected_range': [True, 0, 0, 1, 0, 0, 6],
        'last_selection': {
            'selected_text': '증빙텍스트',
            'selected_text_hash': svc._text_proof_hash('증빙텍스트'),
            'selected_range': [True, 0, 0, 1, 0, 0, 6],
        },
    }

    stable = FakeHwp(collapse_on_read=False)
    proof = svc._capture_selected_text_proof_for_bundle(stable, keep_select=True, selection_cache=selection_cache)
    require(proof['selection_preserved_after_read'] is True, f'stable proof should preserve selection: {proof!r}')
    require(proof['selection_restored'] is False, f'stable proof should not report restoration: {proof!r}')
    require(tuple(proof['selected_range_restored']) == stable.selected, f'stable proof lost range: {proof!r}')
    require(proof['selection_source'] == 'active-selection', f'stable proof should use active selection: {proof!r}')
    require(proof['selected_text_verified_against_cache'] is True, f'stable proof should verify cached proof: {proof!r}')

    collapsing = FakeHwp(collapse_on_read=True)
    proof = svc._capture_selected_text_proof_for_bundle(collapsing, keep_select=True, selection_cache=selection_cache)
    require(proof['selection_preserved_after_read'] is False, f'collapsing proof should notice mutation: {proof!r}')
    require(proof['selection_restored'] is True, f'collapsing proof should restore selection: {proof!r}')
    require(collapsing.selected == (True, 0, 0, 1, 0, 0, 6), f'collapsing proof did not restore live selection: {proof!r}')
    require(any('restored' in warning for warning in proof['warnings']), f'restore warning missing: {proof!r}')

    cached = FakeHwp(collapse_on_read=False, selected=(False, 0, 0, 1, 0, 0, 1))
    proof = svc._capture_selected_text_proof_for_bundle(cached, keep_select=True, selection_cache=selection_cache)
    require(proof['selection_source'] == 'cached-selection-restore', f'cache restore proof used wrong source: {proof!r}')
    require(proof['used_cached_selection'] is True, f'cache restore proof did not mark cached selection: {proof!r}')
    require(proof['had_active_selection_before_restore'] is False, f'cache restore proof should remember missing live selection: {proof!r}')
    require(cached.selected == (True, 0, 0, 1, 0, 0, 6), f'cache restore proof did not restore selected range: {proof!r}')

    mismatched = FakeHwp(collapse_on_read=False, text='다른텍스트', selected=(False, 0, 0, 1, 0, 0, 1))
    try:
        svc._capture_selected_text_proof_for_bundle(mismatched, keep_select=True, selection_cache=selection_cache)
    except LocalCliRuntimeError as exc:
        require('mismatch' in str(exc), f'unexpected mismatch error: {exc}')
    else:
        raise SystemExit('cached selection text mismatch did not fail closed')

    empty = FakeHwp(collapse_on_read=False, text='', selected=(False, 0, 0, 1, 0, 0, 1))
    try:
        svc._capture_selected_text_proof_for_bundle(empty, keep_select=True, selection_cache={
            'selected_range': [True, 0, 0, 1, 0, 0, 6],
            'last_selection': {'selected_range': [True, 0, 0, 1, 0, 0, 6]},
        })
    except LocalCliRuntimeError as exc:
        require('empty text' in str(exc), f'unexpected empty-proof error: {exc}')
    else:
        raise SystemExit('empty selected-text proof did not fail closed')

    no_cache = FakeHwp(collapse_on_read=False, selected=(False, 0, 0, 1, 0, 0, 1))
    try:
        svc._capture_selected_text_proof_for_bundle(no_cache, keep_select=True)
    except LocalCliRuntimeError as exc:
        require('requires an active selection' in str(exc), f'unexpected missing-selection error: {exc}')
    else:
        raise SystemExit('missing active/cached selection did not fail closed')

    clearing = FakeHwp(collapse_on_read=False)
    proof = svc._capture_selected_text_proof_for_bundle(clearing, keep_select=False)
    require(proof['selection_restored'] is False, f'clear-selection should not restore: {proof!r}')
    require(clearing.selected[0] is False, f'clear-selection should allow cleared selection: {proof!r}')

    broken = BrokenRestoreHwp(collapse_on_read=True)
    try:
        svc._capture_selected_text_proof_for_bundle(broken, keep_select=True)
    except LocalCliRuntimeError as exc:
        require('could not restore' in str(exc), f'unexpected fail-closed error: {exc}')
    else:
        raise SystemExit('broken restore did not fail closed')

    select_stable = FakeHwp(collapse_on_read=False, text='doi.org/10.1234/example')
    select_contract = svc._verify_select_live_selection(
        select_stable,
        selected_range=[True, 0, 0, 1, 0, 0, 22],
        selected_text='doi.org/10.1234/example',
        query='doi.org/10.1234/example',
        match_safe_for_type=True,
    )
    require(select_contract['selection_status'] == 'active', f'stable select should be active: {select_contract!r}')
    require(select_contract['active_selection_verified'] is True, f'stable select should verify live selection: {select_contract!r}')
    require(select_contract['safe_for_type'] is True, f'stable select should be safe: {select_contract!r}')

    select_restore = FakeHwp(collapse_on_read=False, text='doi.org/10.1234/example', selected=(False, 0, 0, 1, 0, 0, 1))
    select_contract = svc._verify_select_live_selection(
        select_restore,
        selected_range=[True, 0, 0, 1, 0, 0, 22],
        selected_text='doi.org/10.1234/example',
        query='doi.org/10.1234/example',
        match_safe_for_type=True,
    )
    require(select_contract['restore_attempted'] is True, f'select should try live range restore: {select_contract!r}')
    require(select_contract['active_selection_verified'] is True, f'select restore should verify live selection: {select_contract!r}')
    require(select_restore.selected == (True, 0, 0, 1, 0, 0, 22), f'select restore left wrong range: {select_contract!r}')

    select_broken = BrokenRestoreHwp(collapse_on_read=False, text='doi.org/10.1234/example', selected=(False, 0, 0, 1, 0, 0, 1))
    select_contract = svc._verify_select_live_selection(
        select_broken,
        selected_range=[True, 0, 0, 1, 0, 0, 22],
        selected_text='doi.org/10.1234/example',
        query='doi.org/10.1234/example',
        match_safe_for_type=True,
    )
    require(select_contract['selection_status'] == 'degraded', f'broken select restore should degrade: {select_contract!r}')
    require(select_contract['active_selection_verified'] is False, f'broken select restore must not verify active selection: {select_contract!r}')
    require(select_contract['safe_for_type'] is False, f'broken select restore must not be safe: {select_contract!r}')

    select_anchor = FakeHwp(collapse_on_read=False, text='doi.org')
    select_contract = svc._verify_select_live_selection(
        select_anchor,
        selected_range=[True, 0, 0, 1, 0, 0, 7],
        selected_text='doi.org',
        query='doi.org/10.1234/example',
        match_safe_for_type=False,
    )
    require(select_contract['selection_status'] == 'active-unsafe', f'anchor select should be active but unsafe: {select_contract!r}')
    require(select_contract['active_selection_verified'] is True, f'anchor select should verify live selection: {select_contract!r}')
    require(select_contract['safe_for_type'] is False, f'anchor select must not be type-safe: {select_contract!r}')

    font_proof = svc._build_font_size_proof(
        requested_size_pt=12.0,
        before={'has_selection': True, 'selected_pos': [True, 0, 0, 1, 0, 0, 6], 'pos': [0, 0, 1]},
        after={'has_selection': False, 'selected_pos': [False, 0, 0, 1, 0, 0, 1], 'pos': [0, 0, 7]},
        style_result={'strategy': 'hwp.set_font'},
        context={'current_paragraph_preview': '증빙텍스트 after'},
    )
    require(font_proof['operation'] == 'fontsize', f'font proof operation missing: {font_proof!r}')
    require(font_proof['scope'] == 'current selection', f'font proof scope missing: {font_proof!r}')
    require(font_proof['style']['requested_font_size_pt'] == 12.0, f'font proof requested size missing: {font_proof!r}')
    require(font_proof['style']['applied_font_size_pt'] == 12.0, f'font proof applied size missing: {font_proof!r}')
    require(font_proof['selection_cache_cleared'] is True, f'font proof should mark cache clear: {font_proof!r}')
    require(font_proof['after_paragraph_hash'], f'font proof should hash paragraph preview: {font_proof!r}')

    type_proof = svc._build_type_text_proof(
        inserted_text='새텍스트',
        before={'has_selection': True, 'selected_pos': [True, 0, 0, 1, 0, 0, 6], 'pos': [0, 0, 1]},
        after={'has_selection': False, 'selected_pos': [False, 0, 0, 4, 0, 0, 4], 'pos': [0, 0, 4]},
        mode='replace-selection',
        strategy='Delete+insert_text',
        before_selected_text='증빙텍스트',
        context={'current_paragraph_preview': '새텍스트 after'},
        restored_cached_selection=True,
        native_undo_steps=2,
    )
    require(type_proof['operation'] == 'type', f'type proof operation missing: {type_proof!r}')
    require(type_proof['scope'] == 'replace-selection', f'type proof scope missing: {type_proof!r}')
    require(type_proof['replaced_text_preview'] == '증빙텍스트', f'type proof should include cached replaced preview: {type_proof!r}')
    require(type_proof['replaced_text_hash'] == svc._text_proof_hash('증빙텍스트'), f'type proof replaced hash missing: {type_proof!r}')
    require(type_proof['inserted_text_hash'] == svc._text_proof_hash('새텍스트'), f'type proof inserted hash missing: {type_proof!r}')
    require(type_proof['restored_cached_selection'] is True, f'type proof should mark restored cached selection: {type_proof!r}')
    require(type_proof['selection_cache_cleared'] is True, f'type proof should mark cache clear: {type_proof!r}')

    unknown_replacement_proof = svc._build_type_text_proof(
        inserted_text='새텍스트',
        before={'has_selection': True, 'selected_pos': [True, 0, 0, 1, 0, 0, 6], 'pos': [0, 0, 1]},
        after={'has_selection': False, 'selected_pos': [False, 0, 0, 4, 0, 0, 4], 'pos': [0, 0, 4]},
        mode='replace-selection',
        strategy='Delete+insert_text',
        before_selected_text='',
        context={},
    )
    require(unknown_replacement_proof['replaced_text_known'] is False, f'unknown replacement should not invent proof: {unknown_replacement_proof!r}')
    require(unknown_replacement_proof['selected_text_source'] == 'not-read-before-type-to-preserve-selection', f'unknown replacement source missing: {unknown_replacement_proof!r}')


if __name__ == '__main__':
    main()
