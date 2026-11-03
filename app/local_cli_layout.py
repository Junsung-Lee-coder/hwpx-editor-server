"""Exact layout edits (page setup, columns, sections, hanging indent) for LocalCliService."""

from __future__ import annotations

import time
from typing import Any, Callable

from app.edit_ops import (
    _get_current_paragraph_text_at_cursor,
    _get_pos,
    _get_selected_text,
    _set_pos,
)
from app.hwpml_invariants import before_budget_reason, readback_size_reason
from app.layout_ops import (
    COLDEF_ITEMS,
    COLDEF_REQUIRED,
    OP,
    PAGEDEF_ITEMS,
    PARASHAPE_ITEMS,
    SCHEMA_VERSION,
    REASON_CODES,
    LayoutError,
    compare_documents,
    hwpunit_to_mm,
    normalize_request,
    pagedef_mm,
    parse_layout_xml,
    plan_columns,
    plan_hanging_indent,
    plan_page_setup,
    plan_section_delete,
    plan_section_insert,
    public_document,
    head_pair,
    reason_code,
    verify_columns,
    verify_hanging_indent,
    verify_page_setup,
    verify_section_delete,
    verify_section_insert,
)
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from app.step_refusal import STEP_REFUSAL_CODES, StepControl, restore_caret, strict_pos, strict_state

_ROLLBACK_HINT = {'attempted': False, 'hint': 'Retain the working copy and owning STA; full native close is unsupported for this unproved or held session. Separately authorized exact-owned operator closure is required; a single Undo does not guarantee a full rollback of this edit.'}
# What `hwpx undo` does after a layout edit (_BUNDLE_UNDO_POLICY): refuse.
_UNDO_REPORT = {
    'hwpx_undo': 'refused',
    'undo_units_verified': False,
    'note': 'Retain the working copy and owning STA; full native close is unsupported for this unproved or held session. Separately authorized exact-owned operator closure is required to discard the edit.',
}
# Kinds whose caret position stays meaningful after the edit and is restored.
_RESTORE_CARET_KINDS = frozenset({'page_setup', 'columns', 'hanging_indent'})
_NATIVE_REASONS = frozenset({'native_action_failed', 'native_outcome_unknown', 'readback_after_failed'})
_OPAQUE = ('opaque SDK value',)  # not a scalar: parashape_scalars and the planners ignore it


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

    def _layout_items(self, source: Any, names: tuple[str, ...], *, strict: tuple[str, ...] = ()) -> dict[str, Any]:
        """Item values as plain Python data, so planning never calls back into an SDK-provided object.

        A ``strict`` item must read as an exact int or float; a failing read or any other value is technical.
        Other items keep exact int/float/str/bool/None values; an unreadable one reads as None and any other
        object as an opaque marker that no planner or scalar comparison uses.
        """
        values: dict[str, Any] = {}
        for name in names:
            try:
                value = getattr(source, name)
            except Exception as exc:
                if name in strict:
                    raise LocalCliRuntimeError(f'{OP} could not read {name}: the parameter state is unknown') from exc
                values[name] = None
                continue
            kind = type(value)
            if name in strict:
                if kind is not int and kind is not float:
                    raise LocalCliRuntimeError(f'{OP} {name} did not read as a number: the parameter state is unknown')
                values[name] = value
            else:
                values[name] = value if (kind in (int, float, str, bool) or value is None) else _OPAQUE
        return values

    def _layout_read_pagedef(self, hwp: Any, pset: Any) -> dict[str, Any]:
        self._layout_get_default(hwp, 'PageSetup', pset)
        return self._layout_items(pset.PageDef, PAGEDEF_ITEMS, strict=PAGEDEF_ITEMS)

    def _layout_read_coldef(self, hwp: Any, pset: Any) -> dict[str, Any]:
        self._layout_get_default(hwp, 'MultiColumn', pset)
        return self._layout_items(pset, COLDEF_ITEMS, strict=COLDEF_REQUIRED)

    def _layout_read_parashape(self, hwp: Any, pset: Any) -> dict[str, Any]:
        self._layout_get_default(hwp, 'ParagraphShape', pset)
        names = list(PARASHAPE_ITEMS)
        prop_map = getattr(pset, '_prop_map_get_', None)
        if isinstance(prop_map, dict):
            names += [key for key in prop_map if key != 'HSet' and key not in names]
        return self._layout_items(pset, tuple(names), strict=('Indentation', 'LeftMargin'))

    def _layout_execute(self, hwp: Any, action_name: str, pset: Any, control: StepControl) -> dict[str, Any]:
        """HAction.Execute exactly once. Mutation intent is published before the call."""
        execute = getattr(getattr(hwp, 'HAction', None), 'Execute', None)
        if not callable(execute):
            raise LocalCliRuntimeError(f'{OP} needs HAction.Execute for {action_name}')
        control.begin_mutation()
        try:
            raw = execute(action_name, pset.HSet)
        except Exception as exc:
            return {'action': action_name, 'method': f'HAction.Execute({action_name})', 'succeeded': False, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'}
        return {'action': action_name, 'method': f'HAction.Execute({action_name})', 'succeeded': raw is None or bool(raw), 'result': bool(raw) if raw is not None else None}

    def _layout_native_run(self, hwp: Any, action_name: str, control: StepControl) -> dict[str, Any]:
        control.begin_mutation()
        return {'action': action_name, **self._layout_run_action(hwp, action_name)}

    # ------------------------------------------------------------ state proofs

    def _layout_pos(self, hwp: Any) -> tuple[int, int, int] | None:
        """Lenient caret read for the read-only layout_inspect."""
        try:
            pos = _get_pos(hwp)
            return int(pos[0]), int(pos[1]), int(pos[2])
        except Exception:
            return None

    def _layout_require(self, hwp: Any, expected_pos: tuple[int, int, int], control: StepControl, *, where: str,
                        body_only: bool = False, initial: bool = False) -> dict[str, Any]:
        """Prove normal edit state and the exact caret from strict reads.

        ``initial``: the caller's expected_pos against the untouched editor, so a difference is the caller's stale
        view and a clean refusal. Later re-checks follow the engine's own reads, so any difference is technical.
        """
        state = strict_state(hwp, OP, need_cell=body_only)
        problems = []
        if not state['normal']:
            problems.append(('editor_not_normal', f'{OP} {where}: editor is not provably in normal edit state (selection or mode {state["selection_mode"]})'))
        if body_only and state['is_cell']:
            problems.append(('caret_in_table', f'{OP} {where}: the caret must be in body text, not a table cell'))
        if state['pos'] != tuple(expected_pos):
            problems.append(('caret_moved', f'{OP} {where}: caret is at {list(state["pos"])!r}, expected {list(expected_pos)!r}'))
        if problems:
            code, message = problems[0]
            if initial:
                raise control.refuse(code, message)
            raise LocalCliRuntimeError(message + ' after the engine\'s own reads')
        return state

    def _layout_key_indicator(self, hwp: Any) -> tuple[int | None, int | None]:
        """(section count, current section 1-based) from KeyIndicator, or Nones when unreadable (layout_inspect only)."""
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

    def _layout_key_indicator_strict(self, hwp: Any) -> tuple[int, int]:
        getter = next((getattr(hwp, name) for name in ('KeyIndicator', 'key_indicator') if callable(getattr(hwp, name, None))), None)
        if getter is None:
            raise LocalCliRuntimeError(f'{OP} needs KeyIndicator to place the caret in its section')
        try:
            raw = getter()
        except Exception as exc:
            raise LocalCliRuntimeError(f'{OP} KeyIndicator could not be read; the caret section is unknown') from exc
        if (type(raw) is not tuple and type(raw) is not list) or len(raw) < 3 or type(raw[1]) is not int or type(raw[2]) is not int or raw[1] < 1 or raw[2] < 1:
            raise LocalCliRuntimeError(f'{OP} KeyIndicator returned no exact section numbers; the caret section is unknown')
        return raw[1], raw[2]

    def _layout_document(self, hwp: Any, control: StepControl, *, where: str, body_rule: str | None = None) -> dict[str, Any]:
        get_text = getattr(hwp, 'GetTextFile', None)
        if not callable(get_text):
            raise LocalCliRuntimeError(f'{OP} needs GetTextFile for HWPML readback')
        before = where == 'before edit'
        started = time.monotonic()
        xml_text = get_text('HWPML2X', '')
        if type(xml_text) is not str:
            raise LocalCliRuntimeError(f'{OP} {where}: HWPML readback was not text')
        too_big = readback_size_reason(xml_text)
        if too_big:
            if before:
                raise control.refuse('proof_budget', f'{OP} refused before mutation: {too_big}')
            raise LocalCliRuntimeError(f'{OP} {where}: HWPML readback failed: {too_big}')
        try:
            doc = parse_layout_xml(xml_text)
        except LayoutError as exc:
            raise LocalCliRuntimeError(f'{OP} {where}: HWPML readback failed: {exc}') from exc
        # Every 'before edit' readback runs before the single native action;
        # dry-run the whole-document comparison to time the after-edit proof.
        if before:
            if body_rule is None:
                raise LocalCliRuntimeError(f'{OP} internal error: a before-edit readback needs its body rule for the proof budget')
            compare_documents(doc, doc, body_rule=body_rule)
            slow = before_budget_reason(time.monotonic() - started)
            if slow:
                raise control.refuse('proof_budget', f'{OP} refused before mutation: {slow}')
        return doc

    def _layout_plan(self, control: StepControl, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return build()
        except LayoutError as exc:
            code = getattr(exc, 'code', 'layout_unsupported')
            if code == 'readback_malformed':
                raise LocalCliRuntimeError(f'{OP} a parameter readback was not a usable value: {exc}') from exc
            raise control.refuse(code if code in STEP_REFUSAL_CODES[OP] else 'layout_unsupported', f'{OP} refused before mutation: {exc}') from exc

    @staticmethod
    def _layout_raise_if_failed(result: dict[str, Any]) -> None:
        if not result.get('succeeded'):
            error = LocalCliRuntimeError(f'{OP} native action did not succeed: {result!r}')
            error.step_phase, error.step_reason = 'native', 'native_outcome_unknown' if result.get('outcome_unknown') else 'native_action_failed'
            raise error

    @staticmethod
    def _layout_raise_if_unverified(verification: dict[str, Any]) -> None:
        if not verification['ok']:
            error = LocalCliRuntimeError(f'{OP} refused to mark success: {"; ".join(verification["reasons"])}')
            error.step_phase, error.step_reason = 'verify', reason_code(verification['reasons'][0]) if verification['reasons'] else 'other'
            raise error

    def _layout_after(self, hwp: Any, control: StepControl) -> dict[str, Any]:
        try:
            return self._layout_document(hwp, control, where='after edit')
        except LocalCliRuntimeError as exc:
            exc.step_phase, exc.step_reason = 'readback', 'readback_after_failed'
            raise

    # ------------------------------------------------------------ kinds

    def _layout_page_setup(self, hwp: Any, request: dict[str, Any], control: StepControl) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, control, where='before page setup readback', initial=True)
        pset = self._layout_pset(hwp, 'HSecDef')
        before = self._layout_read_pagedef(hwp, pset)
        doc_before = self._layout_document(hwp, control, where='before edit', body_rule='page_setup')
        _count, caret_section = self._layout_key_indicator_strict(hwp)
        plan = self._layout_plan(control, lambda: plan_page_setup(request, before, doc_before))
        # Fresh defaults for the caret's section, then only the requested items.
        self._layout_get_default(hwp, 'PageSetup', pset)
        for item, value in plan['targets'].items():
            setattr(pset.PageDef, item, value)
        pset.HSet.SetItem('ApplyTo', plan['apply_to_code'])
        self._layout_require(hwp, expected_pos, control, where='before PageSetup')
        result = self._layout_execute(hwp, 'PageSetup', pset, control)
        self._layout_raise_if_failed(result)
        after = self._layout_read_pagedef(hwp, pset)
        doc_after = self._layout_after(hwp, control)
        verification = verify_page_setup(plan, after, doc_before, doc_after, caret_section=caret_section)
        self._layout_raise_if_unverified(verification)
        public_plan = {key: value for key, value in plan.items() if key != 'before'}
        return {'plan': public_plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after), **head_pair(doc_before, doc_after)}

    def _layout_columns(self, hwp: Any, request: dict[str, Any], control: StepControl) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, control, where='before column readback', body_only=True, initial=True)
        pset = self._layout_pset(hwp, 'HColDef')
        before = self._layout_read_coldef(hwp, pset)
        doc_before = self._layout_document(hwp, control, where='before edit', body_rule='columns')
        _count, caret_section = self._layout_key_indicator_strict(hwp)
        plan = self._layout_plan(control, lambda: plan_columns(request, before, doc_before))
        self._layout_get_default(hwp, 'MultiColumn', pset)
        for item, value in plan['targets'].items():
            setattr(pset, item, value)
        pset.HSet.SetItem('ApplyTo', plan['apply_to_code'])
        self._layout_require(hwp, expected_pos, control, where='before MultiColumn', body_only=True)
        result = self._layout_execute(hwp, 'MultiColumn', pset, control)
        self._layout_raise_if_failed(result)
        after = self._layout_read_coldef(hwp, pset)
        doc_after = self._layout_after(hwp, control)
        verification = verify_columns(plan, after, doc_before, doc_after, caret_section=caret_section, caret=expected_pos)
        self._layout_raise_if_unverified(verification)
        return {'plan': plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after), **head_pair(doc_before, doc_after)}

    def _layout_section_insert(self, hwp: Any, request: dict[str, Any], control: StepControl) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, control, where='before section readback', body_only=True, initial=True)
        doc_before = self._layout_document(hwp, control, where='before edit', body_rule='section_insert')
        plan = self._layout_plan(control, lambda: plan_section_insert(doc_before, caret=expected_pos))
        self._layout_require(hwp, expected_pos, control, where='before BreakSection', body_only=True)
        result = self._layout_native_run(hwp, 'BreakSection', control)
        self._layout_raise_if_failed(result)
        doc_after = self._layout_after(hwp, control)
        verification = verify_section_insert(doc_before, doc_after, caret=expected_pos)
        self._layout_raise_if_unverified(verification)
        return {'plan': plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after), **head_pair(doc_before, doc_after)}

    def _layout_prove_section_start(self, hwp: Any, request: dict[str, Any], control: StepControl) -> dict[str, Any]:
        """Read-only proof that the caret opens section ``section_index``.

        KeyIndicator must place the caret in that section, and one MoveLeft
        (caret movement only) must land in the previous section. The caret is
        then put back and re-proved; every read is strict.
        """
        expected_pos, index = request['expected_pos'], request['section_index']
        count, here = self._layout_key_indicator_strict(hwp)
        if here != index:
            raise control.refuse('not_section_start', f'{OP} refused before mutation: KeyIndicator places the caret in section {here}, expected {index}')
        move = self._layout_run_action(hwp, 'MoveLeft')
        if not move.get('succeeded'):
            raise LocalCliRuntimeError(f'{OP} MoveLeft did not report success during the section-start proof')
        moved_pos = strict_pos(hwp, OP)
        _count, previous = self._layout_key_indicator_strict(hwp)
        restore_caret(hwp, OP, tuple(expected_pos))
        proof = {'section_count': count, 'caret_section': here, 'move_left': move, 'moved_pos': list(moved_pos), 'section_after_move_left': previous}
        if moved_pos == tuple(expected_pos) or previous != index - 1:
            raise control.refuse('not_section_start', f'{OP} refused before mutation: the caret is not provably at the start of section {index}')
        return proof

    def _layout_section_delete(self, hwp: Any, request: dict[str, Any], control: StepControl) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        self._layout_require(hwp, expected_pos, control, where='before section-start proof', body_only=True, initial=True)
        proof = self._layout_prove_section_start(hwp, request, control)
        doc_before = self._layout_document(hwp, control, where='before edit', body_rule='section_delete')
        plan = self._layout_plan(control, lambda: plan_section_delete(request, doc_before, key_indicator_sections=proof['section_count']))
        # DeleteBack runs at the proven section start where Hancom keeps the caret: position 0, or the first text after
        # the section's inline definitions (a caret before them is not kept). The readback decides the outcome.
        self._layout_require(hwp, expected_pos, control, where='before DeleteBack', body_only=True)
        result = self._layout_native_run(hwp, 'DeleteBack', control)
        self._layout_raise_if_failed(result)
        doc_after = self._layout_after(hwp, control)
        verification = verify_section_delete(plan, doc_before, doc_after)
        self._layout_raise_if_unverified(verification)
        return {'plan': plan, 'section_start_proof': proof, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after), **head_pair(doc_before, doc_after)}

    def _layout_marker_text(self, hwp: Any, list_id: int, para: int, length: int) -> str:
        select = getattr(hwp, 'select_text', None)
        if not callable(select):
            raise LocalCliRuntimeError(f'{OP} needs select_text to read the marker')
        if select(para, 0, para, length, list_id) is False:
            raise LocalCliRuntimeError(f'{OP} could not select the marker characters')
        text = _get_selected_text(hwp, keep_select=False)
        if type(text) is not str:
            raise LocalCliRuntimeError(f'{OP} the marker text read back was not text')
        return text

    def _layout_hanging_indent(self, hwp: Any, request: dict[str, Any], control: StepControl) -> dict[str, Any]:
        expected_pos = request['expected_pos']
        marker = request['marker_text']
        list_id, para, _pos = expected_pos
        self._layout_require(hwp, expected_pos, control, where='before paragraph readback', initial=True)
        try:
            text_before = _get_current_paragraph_text_at_cursor(hwp)
        except Exception as exc:
            raise LocalCliRuntimeError(f'{OP} could not read the current paragraph: {type(exc).__name__}: {exc}') from exc
        if type(text_before) is not str:
            raise LocalCliRuntimeError(f'{OP} the current paragraph did not read as text')
        self._layout_require(hwp, expected_pos, control, where='after paragraph readback')
        pset = self._layout_pset(hwp, 'HParaShape')
        shape_before = self._layout_read_parashape(hwp, pset)
        plan = self._layout_plan(control, lambda: plan_hanging_indent(request, text_before, shape_before))
        # Prove the first len(marker) characters are the marker itself, then park the caret after it.
        selected = self._layout_marker_text(hwp, list_id, para, len(marker))
        marker_pos = plan['marker_pos']
        restore_caret(hwp, OP, tuple(marker_pos))
        if selected != marker:
            raise control.refuse('marker_mismatch', f'{OP} refused before mutation: the first {len(marker)} characters of the paragraph are not marker_text')
        self._layout_require(hwp, marker_pos, control, where='after placing the caret behind the marker')
        doc_before = self._layout_document(hwp, control, where='before edit', body_rule='hanging_indent')
        self._layout_require(hwp, marker_pos, control, where='before ParagraphShapeIndentAtCaret')
        result = self._layout_native_run(hwp, 'ParagraphShapeIndentAtCaret', control)
        self._layout_raise_if_failed(result)
        pos_after = self._layout_pos(hwp)
        if pos_after != tuple(marker_pos):
            raise LocalCliRuntimeError(f'{OP} caret moved to {pos_after!r} during ParagraphShapeIndentAtCaret; the paragraph shape readback would not be bound to the target')
        shape_after = self._layout_read_parashape(hwp, pset)
        text_after = _get_current_paragraph_text_at_cursor(hwp)
        doc_after = self._layout_after(hwp, control)
        verification = verify_hanging_indent(plan, shape_after, text_before, text_after, doc_before, doc_after)
        self._layout_raise_if_unverified(verification)
        public_plan = {'kind': plan['kind'], 'marker_chars': len(marker), 'marker_pos': list(marker_pos)}
        return {'plan': public_plan, 'native_actions': [result], 'verification': verification,
                'document_before': public_document(doc_before), 'document_after': public_document(doc_after), **head_pair(doc_before, doc_after)}

    # ------------------------------------------------------------ entry point

    def _bundle_layout_exact(self, hwp: Any, step: dict[str, Any], control: StepControl | None = None) -> dict[str, Any]:
        control = control if control is not None else StepControl(OP)
        try:
            request = normalize_request(step)
        except LayoutError as exc:
            refusal = control.refuse('invalid_request', f'{OP} refused: {exc}')
            control.register(refusal)
            raise refusal from exc
        kind = request['kind']
        handler = {
            'page_setup': self._layout_page_setup,
            'columns': self._layout_columns,
            'section_insert': self._layout_section_insert,
            'section_delete': self._layout_section_delete,
            'hanging_indent': self._layout_hanging_indent,
        }[kind]
        original_pos = strict_pos(hwp, OP)
        try:
            body = handler(hwp, request, control)
        except Exception as exc:
            if control.is_pending(exc):
                # A clean refusal leaves the caret exactly where it was; prove that before handing it over.
                restore_caret(hwp, OP, original_pos)
                control.register(exc)
                raise
            if not control.started:
                # Technical failure before any mutator: no restoration or further SDK call.
                raise
            message = str(exc) if isinstance(exc, LocalCliRuntimeError) else f'{OP} failed after native mutation started: {type(exc).__name__}: {exc}'
            rollback = dict(_ROLLBACK_HINT)
            rollback['undo'] = dict(_UNDO_REPORT)
            error = LocalCliMutationError(message, mutation_may_have_persisted=True, rollback=rollback)
            phase, reason = getattr(exc, 'step_phase', None), getattr(exc, 'step_reason', None)
            if type(phase) is str and type(reason) is str and phase in ('native', 'readback', 'verify') and (reason in REASON_CODES or reason in _NATIVE_REASONS):
                error.step_phase, error.step_reason = phase, reason
            else:
                error.step_phase, error.step_reason = 'after_mutation', 'other'
            raise error from exc
        # Section edits renumber positions, so after one ran the caret stays where Hancom left it.
        if kind in _RESTORE_CARET_KINDS:
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
            'undo': dict(_UNDO_REPORT),
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
