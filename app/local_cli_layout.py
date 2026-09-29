"""Exact layout edits (page setup, columns, sections, hanging indent) for LocalCliService."""

from __future__ import annotations

import time
from typing import Any, Callable

from app.edit_ops import _get_current_paragraph_text_at_cursor, _get_pos, _get_selected_text, _set_pos
from app.hwpml_invariants import before_budget_reason, readback_size_reason
from app.layout_ops import (
    COLDEF_ITEMS,
    OP,
    PAGEDEF_ITEMS,
    PARASHAPE_ITEMS,
    SCHEMA_VERSION,
    LayoutError,
    hwpunit_to_mm,
    pagedef_mm,
    normalize_request,
    parse_layout_xml,
    plan_columns,
    plan_hanging_indent,
    plan_page_setup,
    plan_section_delete,
    plan_section_insert,
    public_document,
    strict_normal,
    verify_columns,
    verify_hanging_indent,
    verify_page_setup,
    verify_section_delete,
    verify_section_insert,
)
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError

_ROLLBACK_HINT = {'attempted': False, 'hint': 'Inspect rendered proof; use undo or reopen the working copy before saving.'}
# Kinds whose caret position stays meaningful after the edit and is restored.
_RESTORE_CARET_KINDS = frozenset({'page_setup', 'columns', 'hanging_indent'})


class _Mutation:
    """Tracks whether a native mutating action has been issued."""

    started = False


class LocalCliLayoutMixin:
    """Exact layout edits for LocalCliService.

    Before the one mutating native action, the editor must be provably in
    normal edit state (no selection, selection mode 0; unreadable counts as
    not normal) with the caret exactly on the step's ``expected_pos``. Every
    kind reads the relevant parameter set and the whole-document HWPML before
    and after, and succeeds only when the readback shows exactly the planned
    change. Once a mutating action was issued any failure is raised as
    LocalCliMutationError(mutation_may_have_persisted=True).
    """

    # ------------------------------------------------------------ native primitives

    def _layout_run_action(self, hwp: Any, action_name: str) -> dict[str, Any]:
        """Run one Hancom action exactly once; hwp.Run only when HAction.Run is absent; never retried."""
        haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        label, runner = f'HAction.Run({action_name})', haction_run
        if not callable(runner):
            label, runner = f'Run({action_name})', getattr(hwp, 'Run', None)
        if not callable(runner):
            return {'succeeded': False, 'method': None, 'error': f'no entry point for {action_name}'}
        try:
            raw = runner(action_name)
        except Exception as exc:
            return {'succeeded': False, 'method': label, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'}
        return {'succeeded': raw is None or bool(raw), 'method': label, 'result': bool(raw) if raw is not None else None}

    def _layout_pset(self, hwp: Any, name: str) -> Any:
        try:
            pset = getattr(hwp.HParameterSet, name)
            hset = pset.HSet
        except Exception as exc:
            raise LocalCliRuntimeError(f'{OP} needs HParameterSet.{name}: {type(exc).__name__}: {exc}') from exc
        if hset is None:
            raise LocalCliRuntimeError(f'{OP} HParameterSet.{name} exposes no HSet')
        return pset

    def _layout_get_default(self, hwp: Any, action_name: str, pset: Any) -> None:
        get_default = getattr(getattr(hwp, 'HAction', None), 'GetDefault', None)
        if not callable(get_default):
            raise LocalCliRuntimeError(f'{OP} needs HAction.GetDefault for {action_name} readback')
        get_default(action_name, pset.HSet)

    def _layout_items(self, source: Any, names: tuple[str, ...]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in names:
            try:
                values[name] = getattr(source, name)
            except Exception:
                values[name] = None
        return values

    def _layout_read_pagedef(self, hwp: Any, pset: Any) -> dict[str, Any]:
        self._layout_get_default(hwp, 'PageSetup', pset)
        return self._layout_items(pset.PageDef, PAGEDEF_ITEMS)

    def _layout_read_coldef(self, hwp: Any, pset: Any) -> dict[str, Any]:
        self._layout_get_default(hwp, 'MultiColumn', pset)
        return self._layout_items(pset, COLDEF_ITEMS)

    def _layout_read_parashape(self, hwp: Any, pset: Any) -> dict[str, Any]:
        self._layout_get_default(hwp, 'ParagraphShape', pset)
        names = list(PARASHAPE_ITEMS)
        prop_map = getattr(pset, '_prop_map_get_', None)
        if isinstance(prop_map, dict):
            names += [key for key in prop_map if key != 'HSet' and key not in names]
        return self._layout_items(pset, tuple(names))

    def _layout_execute(self, hwp: Any, action_name: str, pset: Any, mutation: _Mutation) -> dict[str, Any]:
        """HAction.Execute exactly once. The mutation flag is set before the call."""
        execute = getattr(getattr(hwp, 'HAction', None), 'Execute', None)
        if not callable(execute):
            raise LocalCliRuntimeError(f'{OP} needs HAction.Execute for {action_name}')
        mutation.started = True
        try:
            raw = execute(action_name, pset.HSet)
        except Exception as exc:
            return {'action': action_name, 'method': f'HAction.Execute({action_name})', 'succeeded': False, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'}
        return {'action': action_name, 'method': f'HAction.Execute({action_name})', 'succeeded': raw is None or bool(raw), 'result': bool(raw) if raw is not None else None}

    def _layout_native_run(self, hwp: Any, action_name: str, mutation: _Mutation) -> dict[str, Any]:
        mutation.started = True
        return {'action': action_name, **self._layout_run_action(hwp, action_name)}

    # ------------------------------------------------------------ state proofs

    def _layout_pos(self, hwp: Any) -> tuple[int, int, int] | None:
        try:
            pos = _get_pos(hwp)
            return int(pos[0]), int(pos[1]), int(pos[2])
        except Exception:
            return None

    def _layout_require(self, hwp: Any, expected_pos: tuple[int, int, int], *, where: str, body_only: bool = False) -> dict[str, Any]:
        """Re-prove normal edit state and the exact caret; raise if either is not proven."""
        snapshot = self._bundle_compact_snapshot(hwp)  # type: ignore[attr-defined]
        if not strict_normal(snapshot):
            raise LocalCliRuntimeError(f'{OP} {where}: editor is not provably in normal edit state: {snapshot!r}')
        if body_only and snapshot.get('is_cell') is not False:
            raise LocalCliRuntimeError(f'{OP} {where}: the caret must be in body text, not a table cell (is_cell={snapshot.get("is_cell")!r})')
        pos = self._layout_pos(hwp)
        if pos != tuple(expected_pos):
            raise LocalCliRuntimeError(f'{OP} {where}: caret is at {pos!r}, expected {list(expected_pos)!r}')
        return snapshot

    def _layout_key_indicator(self, hwp: Any) -> tuple[int | None, int | None]:
        """(section count, current section 1-based) from KeyIndicator, or Nones when unreadable."""
        for name in ('KeyIndicator', 'key_indicator'):
            getter = getattr(hwp, name, None)
            if not callable(getter):
                continue
            try:
                raw = getter()
            except Exception:
                continue
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                try:
                    return int(raw[1]), int(raw[2])
                except Exception:
                    return None, None
        return None, None

    def _layout_document(self, hwp: Any, *, where: str) -> dict[str, Any]:
        get_text = getattr(hwp, 'GetTextFile', None)
        if not callable(get_text):
            raise LocalCliRuntimeError(f'{OP} needs GetTextFile for HWPML readback')
        started = time.monotonic()
        try:
            xml_text = get_text('HWPML2X', '')
            too_big = readback_size_reason(xml_text)
            if too_big:
                raise LayoutError(too_big)
            doc = parse_layout_xml(xml_text)
        except LayoutError as exc:
            raise LocalCliRuntimeError(f'{OP} {where}: HWPML readback failed: {exc}') from exc
        # Every 'before edit' readback runs before the single native action.
        slow = before_budget_reason(time.monotonic() - started) if where == 'before edit' else None
        if slow:
            raise LocalCliRuntimeError(f'{OP} refused before mutation: {slow}')
        return doc

    def _layout_plan(self, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return build()
        except LayoutError as exc:
            raise LocalCliRuntimeError(f'{OP} refused before mutation: {exc}') from exc

    @staticmethod
    def _layout_raise_if_failed(result: dict[str, Any]) -> None:
        if not result.get('succeeded'):
            raise LocalCliRuntimeError(f'{OP} native action did not succeed: {result!r}')

    @staticmethod
    def _layout_raise_if_unverified(verification: dict[str, Any]) -> None:
        if not verification['ok']:
            raise LocalCliRuntimeError(f'{OP} refused to mark success: {"; ".join(verification["reasons"])}')

    # ------------------------------------------------------------ kinds

    def _layout_page_setup(self, hwp: Any, request: dict[str, Any], mutation: _Mutation) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, where='before page setup readback')
        pset = self._layout_pset(hwp, 'HSecDef')
        before = self._layout_read_pagedef(hwp, pset)
        doc_before = self._layout_document(hwp, where='before edit')
        _count, caret_section = self._layout_key_indicator(hwp)
        plan = self._layout_plan(lambda: plan_page_setup(request, before, doc_before))
        # Fresh defaults for the caret's section, then only the requested items.
        self._layout_get_default(hwp, 'PageSetup', pset)
        for item, value in plan['targets'].items():
            setattr(pset.PageDef, item, value)
        pset.HSet.SetItem('ApplyTo', plan['apply_to_code'])
        self._layout_require(hwp, expected_pos, where='before PageSetup')
        result = self._layout_execute(hwp, 'PageSetup', pset, mutation)
        self._layout_raise_if_failed(result)
        after = self._layout_read_pagedef(hwp, pset)
        doc_after = self._layout_document(hwp, where='after edit')
        verification = verify_page_setup(plan, after, doc_before, doc_after, caret_section=caret_section)
        self._layout_raise_if_unverified(verification)
        public_plan = {key: value for key, value in plan.items() if key != 'before'}
        return {'plan': public_plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after)}

    def _layout_columns(self, hwp: Any, request: dict[str, Any], mutation: _Mutation) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, where='before column readback', body_only=True)
        pset = self._layout_pset(hwp, 'HColDef')
        before = self._layout_read_coldef(hwp, pset)
        doc_before = self._layout_document(hwp, where='before edit')
        _count, caret_section = self._layout_key_indicator(hwp)
        if doc_before['section_count'] > 1 and caret_section is None:
            raise LocalCliRuntimeError(f'{OP} refused before mutation: KeyIndicator does not report the caret section, so a column change cannot be scoped')
        plan = self._layout_plan(lambda: plan_columns(request, before, doc_before))
        self._layout_get_default(hwp, 'MultiColumn', pset)
        for item, value in plan['targets'].items():
            setattr(pset, item, value)
        pset.HSet.SetItem('ApplyTo', plan['apply_to_code'])
        self._layout_require(hwp, expected_pos, where='before MultiColumn', body_only=True)
        result = self._layout_execute(hwp, 'MultiColumn', pset, mutation)
        self._layout_raise_if_failed(result)
        after = self._layout_read_coldef(hwp, pset)
        doc_after = self._layout_document(hwp, where='after edit')
        verification = verify_columns(plan, after, doc_before, doc_after, caret_section=caret_section)
        self._layout_raise_if_unverified(verification)
        return {'plan': plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after)}

    def _layout_section_insert(self, hwp: Any, request: dict[str, Any], mutation: _Mutation) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, where='before section readback', body_only=True)
        doc_before = self._layout_document(hwp, where='before edit')
        plan = self._layout_plan(lambda: plan_section_insert(doc_before))
        self._layout_require(hwp, expected_pos, where='before BreakSection', body_only=True)
        result = self._layout_native_run(hwp, 'BreakSection', mutation)
        self._layout_raise_if_failed(result)
        doc_after = self._layout_document(hwp, where='after edit')
        verification = verify_section_insert(doc_before, doc_after)
        self._layout_raise_if_unverified(verification)
        return {'plan': plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after)}

    def _layout_prove_section_start(self, hwp: Any, request: dict[str, Any]) -> dict[str, Any]:
        """Read-only proof that the caret opens section ``section_index``.

        KeyIndicator must place the caret in that section, and one MoveLeft
        (caret movement only) must land in the previous section. The caret is
        then put back and re-checked.
        """
        expected_pos, index = request['expected_pos'], request['section_index']
        count, here = self._layout_key_indicator(hwp)
        if here != index:
            raise LocalCliRuntimeError(f'{OP} refused before mutation: KeyIndicator places the caret in section {here!r}, expected {index}')
        move = self._layout_run_action(hwp, 'MoveLeft')
        moved_pos = self._layout_pos(hwp)
        _count, previous = self._layout_key_indicator(hwp)
        _set_pos(hwp, *expected_pos)
        proof = {'section_count': count, 'caret_section': here, 'move_left': move, 'moved_pos': moved_pos, 'section_after_move_left': previous}
        if not move.get('succeeded') or moved_pos in (None, tuple(expected_pos)) or previous != index - 1:
            raise LocalCliRuntimeError(f'{OP} refused before mutation: the caret is not provably at the start of section {index}: {proof!r}')
        return proof

    def _layout_section_delete(self, hwp: Any, request: dict[str, Any], mutation: _Mutation) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, where='before section-start proof', body_only=True)
        proof = self._layout_prove_section_start(hwp, request)
        doc_before = self._layout_document(hwp, where='before edit')
        plan = self._layout_plan(lambda: plan_section_delete(request, doc_before, key_indicator_sections=proof['section_count']))
        self._layout_require(hwp, expected_pos, where='before DeleteBack', body_only=True)
        result = self._layout_native_run(hwp, 'DeleteBack', mutation)
        self._layout_raise_if_failed(result)
        doc_after = self._layout_document(hwp, where='after edit')
        verification = verify_section_delete(plan, doc_before, doc_after)
        self._layout_raise_if_unverified(verification)
        return {'plan': plan, 'section_start_proof': proof, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after)}

    def _layout_marker_text(self, hwp: Any, list_id: int, para: int, length: int) -> str | None:
        select = getattr(hwp, 'select_text', None)
        if not callable(select):
            return None
        try:
            if select(para, 0, para, length, list_id) is False:
                return None
            return _get_selected_text(hwp, keep_select=False)
        except Exception:
            return None

    def _layout_hanging_indent(self, hwp: Any, request: dict[str, Any], mutation: _Mutation) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        marker = request['marker_text']
        list_id, para, _pos = expected_pos
        self._layout_require(hwp, expected_pos, where='before paragraph readback')
        try:
            text_before = _get_current_paragraph_text_at_cursor(hwp)
        except Exception as exc:
            raise LocalCliRuntimeError(f'{OP} could not read the current paragraph: {type(exc).__name__}: {exc}') from exc
        self._layout_require(hwp, expected_pos, where='after paragraph readback')
        pset = self._layout_pset(hwp, 'HParaShape')
        shape_before = self._layout_read_parashape(hwp, pset)
        plan = self._layout_plan(lambda: plan_hanging_indent(request, text_before, shape_before))
        # Prove the first len(marker) characters are the marker itself, then park the caret after it.
        selected = self._layout_marker_text(hwp, list_id, para, len(marker))
        marker_pos = plan['marker_pos']
        _set_pos(hwp, *marker_pos)
        if selected != marker:
            raise LocalCliRuntimeError(f'{OP} refused before mutation: characters 0..{len(marker)} of the paragraph read {selected!r}, not marker_text {marker!r}')
        self._layout_require(hwp, marker_pos, where='after placing the caret behind the marker')
        doc_before = self._layout_document(hwp, where='before edit')
        self._layout_require(hwp, marker_pos, where='before ParagraphShapeIndentAtCaret')
        result = self._layout_native_run(hwp, 'ParagraphShapeIndentAtCaret', mutation)
        self._layout_raise_if_failed(result)
        pos_after = self._layout_pos(hwp)
        if pos_after != tuple(marker_pos):
            raise LocalCliRuntimeError(f'{OP} caret moved to {pos_after!r} during ParagraphShapeIndentAtCaret; the paragraph shape readback would not be bound to the target')
        shape_after = self._layout_read_parashape(hwp, pset)
        text_after = _get_current_paragraph_text_at_cursor(hwp)
        doc_after = self._layout_document(hwp, where='after edit')
        verification = verify_hanging_indent(plan, shape_after, text_before, text_after, doc_before, doc_after)
        self._layout_raise_if_unverified(verification)
        public_plan = {'kind': plan['kind'], 'marker_chars': len(marker), 'marker_pos': list(marker_pos)}
        return {'plan': public_plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after)}

    # ------------------------------------------------------------ entry point

    def _bundle_layout_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        try:
            request = normalize_request(step)
        except LayoutError as exc:
            raise LocalCliRuntimeError(f'{OP} refused: {exc}') from exc
        kind = request['kind']
        handler = {
            'page_setup': self._layout_page_setup,
            'columns': self._layout_columns,
            'section_insert': self._layout_section_insert,
            'section_delete': self._layout_section_delete,
            'hanging_indent': self._layout_hanging_indent,
        }[kind]
        original_pos = self._layout_pos(hwp)
        mutation = _Mutation()
        try:
            body = handler(hwp, request, mutation)
        except Exception as exc:
            if not mutation.started:
                raise
            message = str(exc) if isinstance(exc, LocalCliRuntimeError) else f'{OP} failed after native mutation started: {type(exc).__name__}: {exc}'
            raise LocalCliMutationError(message, mutation_may_have_persisted=True, rollback=dict(_ROLLBACK_HINT)) from exc
        finally:
            # Section edits renumber positions, so after one ran the caret stays where Hancom left it.
            if original_pos is not None and (kind in _RESTORE_CARET_KINDS or not mutation.started):
                try:
                    _set_pos(hwp, *original_pos)
                except Exception:
                    pass
        return {
            'schema_version': SCHEMA_VERSION,
            'succeeded': True,
            'kind': kind,
            'expected_pos': list(request['expected_pos']),
            **body,
            'next_proof_required': 'Render the affected pages (page-screenshot or export-proof-range) and review them before saving; re-probe positions and layout before another exact edit.',
            'warnings': [],
        }

    def _bundle_layout_inspect(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        """Read-only: current page setup and column definition at the caret, in the units layout_exact takes.

        Only GetDefault is called (no Execute), so nothing is applied. Use the
        values as ``expected_before`` for a later ``layout_exact`` step.
        """
        pos = self._layout_pos(hwp)
        sections, current = self._layout_key_indicator(hwp)
        pagedef_raw = self._layout_read_pagedef(hwp, self._layout_pset(hwp, 'HSecDef'))
        missing = [item for item in PAGEDEF_ITEMS if not isinstance(pagedef_raw.get(item), (int, float)) or isinstance(pagedef_raw.get(item), bool)]
        if missing:
            raise LocalCliRuntimeError(f'layout_inspect could not read PageDef items: {", ".join(missing)}')
        page = pagedef_mm({item: int(pagedef_raw[item]) for item in PAGEDEF_ITEMS})
        columns: dict[str, Any]
        try:
            coldef_raw = self._layout_read_coldef(hwp, self._layout_pset(hwp, 'HColDef'))
            count, same_size, gap = coldef_raw.get('Count'), coldef_raw.get('SameSize'), coldef_raw.get('SameGap')
            columns = {
                'count': int(count) if isinstance(count, (int, float)) else None,
                'same_width': bool(same_size) if same_size is not None else None,
                'gap_mm': hwpunit_to_mm(gap) if isinstance(gap, (int, float)) else None,
            }
        except LocalCliRuntimeError as exc:
            columns = {'error': str(exc)}
        return {
            'schema_version': 'local-cli/layout-inspect/v1',
            'read_only': True,
            'caret_pos': list(pos) if pos is not None else None,
            'section': {'count': sections, 'current': current},
            'page_setup': page,
            'columns': columns,
            'use_as': 'expected_before for layout_exact (page_setup: the fields you change; columns: count/same_width/gap_mm)',
            'warnings': [],
        }
