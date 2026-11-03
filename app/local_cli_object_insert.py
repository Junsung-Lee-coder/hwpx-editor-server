"""Exact single-object insertion at the caret for LocalCliService."""

from __future__ import annotations

import time
from typing import Any

from app.edit_ops import _get_pos, _get_selected_text
from app.hwpml_invariants import before_budget_reason, head_summary, readback_size_reason
from app.local_cli_runtime import LocalCliRuntimeError, insert_text_at_caret
from app.local_cli_service_support import LocalCliMutationError
from app.step_refusal import STEP_REFUSAL_CODES, StepControl, strict_state
from app.object_insert import (
    CLOSE_ACTION,
    OP,
    SCHEMA_VERSION,
    SHAPE_KINDS,
    ObjectInsertError,
    check_before,
    dry_run_proof,
    evaluate_insert,
    native_items,
    normalize_step,
    parse_hwpml,
    public_counts,
    public_plan,
    reason_code,
    summarize,
)

_ROLLBACK_HINT = {'attempted': False, 'hint': 'Retain the working copy and owning STA; full native close is unsupported for this unproved or held session. Separately authorized exact-owned operator closure is required; a single Undo does not guarantee a full rollback of this edit.'}
_UNDO_NOTE = (
    'hwpx undo refuses after object_insert_exact (_BUNDLE_UNDO_POLICY): Hancom may record each native editing action '
    'as its own undo unit and the grouping is not verified natively. Retain the working copy and owning STA; full native close is unsupported for this unproved or held session. Separately authorized exact-owned operator closure is required to discard the edit.'
)


class _Mutation:
    """Tracks whether a native mutating action has been issued, and how many editing actions ran."""

    started = False
    editing_actions = 0


def _undo_report(mutation: _Mutation) -> dict[str, Any]:
    """What `hwpx undo` will do after this edit: refuse, whatever the action count."""
    return {
        'native_editing_actions': mutation.editing_actions,
        'hwpx_undo': 'refused',
        'undo_units_verified': False,
        'note': _UNDO_NOTE,
    }


class LocalCliObjectInsertMixin:
    """Insert exactly one footnote/endnote/memo/hyperlink/bookmark/equation/shape/header/footer.

    The whole document is read as HWPML before and after. Before the single
    mutating native action the editor must be provably in the state the kind
    needs (normal edit state, or a live selection whose text is proven for a
    hyperlink) and the caret must sit on ``expected_pos``. Success is claimed
    only when the readback proves exactly one expected object was added and
    nothing else changed; any failure once the native action was issued is a
    LocalCliMutationError with mutation_may_have_persisted=True.
    """

    def _object_insert_run(self, hwp: Any, action_name: str) -> dict[str, Any]:
        """Run one Hancom action exactly once (hwp.Run only if HAction.Run does not exist)."""
        runner = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        label = f'HAction.Run({action_name})'
        if not callable(runner):
            label, runner = f'Run({action_name})', getattr(hwp, 'Run', None)
        if not callable(runner):
            return {'action': action_name, 'succeeded': False, 'method': None, 'error': f'no entry point for {action_name}'}
        try:
            raw = runner(action_name)
        except Exception as exc:
            return {'action': action_name, 'succeeded': False, 'method': label, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'}
        return {'action': action_name, 'succeeded': raw is None or bool(raw), 'method': label, 'result': bool(raw) if raw is not None else None}

    def _object_insert_prepare_pset(self, hwp: Any, action_name: str, pset_name: str, items: dict[str, Any], *,
                                    by_property: bool = False) -> tuple[Any, Any]:
        """GetDefault, then each item through HSet.SetItem (or the parameter-set property when ``by_property``), for an
        Execute action. Not mutating; raises LocalCliRuntimeError."""
        haction = getattr(hwp, 'HAction', None)
        get_default = getattr(haction, 'GetDefault', None)
        execute = getattr(haction, 'Execute', None)
        pset = getattr(getattr(hwp, 'HParameterSet', None), pset_name, None)
        hset = getattr(pset, 'HSet', None)
        if not callable(get_default) or not callable(execute) or hset is None:
            raise LocalCliRuntimeError(f'{OP} needs HAction.GetDefault/Execute and HParameterSet.{pset_name} for {action_name}')
        try:
            get_default(action_name, hset)
            for name, value in items.items():
                if by_property:
                    setattr(pset, name, value)
                else:
                    hset.SetItem(name, value)
        except Exception as exc:
            raise LocalCliRuntimeError(f'{OP} could not prepare {pset_name} for {action_name}: {type(exc).__name__}: {exc}') from exc
        return execute, hset

    def _object_insert_execute(self, execute: Any, hset: Any, action_name: str) -> dict[str, Any]:
        label = f'HAction.Execute({action_name})'
        try:
            raw = execute(action_name, hset)
        except Exception as exc:
            return {'action': action_name, 'succeeded': False, 'method': label, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'}
        return {'action': action_name, 'succeeded': raw is None or bool(raw), 'method': label, 'result': bool(raw) if raw is not None else None}

    def _object_insert_readback(self, hwp: Any, control: StepControl, *, where: str) -> Any:
        get_text = getattr(hwp, 'GetTextFile', None)
        if not callable(get_text):
            raise LocalCliRuntimeError(f'{OP} {where}: GetTextFile is unavailable, so the document cannot be read back')
        try:
            xml_text = get_text('HWPML2X', '')
        except Exception as exc:
            raise LocalCliRuntimeError(f'{OP} {where}: HWPML readback failed: {type(exc).__name__}: {exc}') from exc
        if type(xml_text) is not str:
            raise LocalCliRuntimeError(f'{OP} {where}: HWPML readback was not text')
        too_big = readback_size_reason(xml_text)
        if too_big:
            if where == 'before insert':
                raise control.refuse('proof_budget', f'{OP} refused before mutation: {too_big}')
            raise LocalCliRuntimeError(f'{OP} {where}: {too_big}')
        try:
            return parse_hwpml(xml_text)
        except ObjectInsertError as exc:
            raise LocalCliRuntimeError(f'{OP} {where}: {exc}') from exc

    def _object_insert_require(self, hwp: Any, plan: dict[str, Any], control: StepControl, *, where: str,
                               initial: bool = False) -> dict[str, Any]:
        """Prove edit state, caret position and (hyperlink) the selected text from strict reads.

        ``initial``: against the untouched editor, so a difference is the caller's stale view and a clean refusal.
        Later re-checks follow the engine's own reads or parameter preparation, so any difference is technical.
        """
        state = strict_state(hwp, OP)
        policy = plan['selection_policy']
        problems = []
        if policy == 'none' and not state['normal']:
            problems.append(('editor_not_normal', f'{OP} {where}: editor is not provably in normal edit state'))
        if policy == 'required' and not state['selected']:
            problems.append(('selection_required', f'{OP} {where}: {plan["kind"]} needs a live selection whose text is display_text'))
        if policy == 'optional' and not (state['normal'] or state['selected']):
            problems.append(('editor_not_normal', f'{OP} {where}: editor is neither in normal edit state nor holding a selection'))
        if list(state['pos']) != plan['expected_pos']:
            problems.append(('caret_moved', f'{OP} {where}: caret is at {list(state["pos"])!r}, expected {plan["expected_pos"]!r}; re-run where'))
        if problems:
            code, message = problems[0]
            if initial:
                raise control.refuse(code, message)
            raise LocalCliRuntimeError(message + " after the engine's own reads")
        proof: dict[str, Any] = {'snapshot': {'pos': list(state['pos']), 'selection_mode': state['selection_mode'],
                                              'has_selection': state['selected']}, 'pos': list(state['pos'])}
        if plan['kind'] == 'hyperlink':
            try:
                selected = _get_selected_text(hwp, keep_select=True)
            except Exception as exc:
                raise LocalCliRuntimeError(f'{OP} {where}: selected text is unreadable: {type(exc).__name__}') from exc
            if type(selected) is not str:
                raise LocalCliRuntimeError(f'{OP} {where}: selected text read back was not text')
            after = strict_state(hwp, OP)
            if after['selection'] != state['selection'] or after['pos'] != state['pos']:
                raise LocalCliRuntimeError(f'{OP} {where}: selection or caret changed while its text was read')
            if selected != plan['display_text']:
                if initial:
                    raise control.refuse('selection_text_mismatch', f'{OP} {where}: selected text ({len(selected)} chars) is not exactly display_text ({len(plan["display_text"])} chars)')
                raise LocalCliRuntimeError(f'{OP} {where}: selected text changed after it was proved')
            proof['selected_range'] = [True, *state['selection']]
        return proof

    def _object_insert_pos(self, hwp: Any) -> list[int] | None:
        try:
            pos = _get_pos(hwp)
            return [int(item) for item in pos[:3]] if len(pos) >= 3 else None
        except Exception:
            return None

    def _object_insert_normal_after(self, hwp: Any) -> bool:
        """After the mutator: is the editor back in normal state? (Unreadable counts as not normal.)"""
        try:
            return strict_state(hwp, OP)['normal']
        except LocalCliRuntimeError:
            return False

    def _object_insert_mutate(self, hwp: Any, plan: dict[str, Any], control: StepControl, mutation: _Mutation) -> list[dict[str, Any]]:
        native = plan['native']
        actions: list[dict[str, Any]] = []
        if native['mode'] == 'run':
            self._object_insert_require(hwp, plan, control, where=f'before {native["action"]}')
            control.begin_mutation()
            mutation.started = True
            mutation.editing_actions += 1
            result = self._object_insert_run(hwp, native['action'])
        else:
            execute, hset = self._object_insert_prepare_pset(hwp, native['action'], native['pset'], native_items(plan),
                                                             by_property=native.get('set_by') == 'property')
            self._object_insert_require(hwp, plan, control, where=f'before {native["action"]}')
            control.begin_mutation()
            mutation.started = True
            mutation.editing_actions += 1
            result = self._object_insert_execute(execute, hset, native['action'])
        actions.append(result)
        if not result['succeeded']:
            error = LocalCliRuntimeError(f'{OP} native action did not succeed: {result!r}')
            error.step_phase, error.step_reason = 'native', 'native_outcome_unknown' if result.get('outcome_unknown') else 'native_action_failed'
            raise error
        if native.get('types_text'):
            mutation.editing_actions += 1
            try:
                insert_text_at_caret(hwp, plan['text'])
                actions.append({'action': 'InsertText', 'succeeded': True, 'chars': len(plan['text'])})
            except Exception as exc:
                actions.append({'action': 'InsertText', 'succeeded': False, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'})
                error = LocalCliRuntimeError(f'{OP} typing into the new {plan["kind"]} failed: {actions!r}')
                error.step_phase, error.step_reason = 'native', 'typing_failed'
                raise error from exc
        if native.get('closes'):
            closed = self._object_insert_run(hwp, CLOSE_ACTION)
            actions.append(closed)
            pos = self._object_insert_pos(hwp)
            if not closed['succeeded'] or pos is None or pos[0] != plan['expected_pos'][0]:
                error = LocalCliRuntimeError(f'{OP} could not return to the original list after editing the {plan["kind"]} (caret {pos!r}): {closed!r}')
                error.step_phase, error.step_reason = 'native', 'close_return_failed'
                raise error
        elif plan['kind'] in SHAPE_KINDS or plan['kind'] == 'equation':
            # A fresh drawing object may stay selected (or in drawing mode); Esc once.
            if not self._object_insert_normal_after(hwp):
                actions.append(self._object_insert_run(hwp, 'Cancel'))
        return actions

    def _bundle_object_insert_exact(self, hwp: Any, step: dict[str, Any], control: StepControl | None = None) -> dict[str, Any]:
        control = control if control is not None else StepControl(OP)
        try:
            plan = normalize_step(step)
        except ObjectInsertError as exc:
            refusal = control.refuse('invalid_request', f'{OP} refused: {exc}')
            control.register(refusal)
            raise refusal from exc
        mutation = _Mutation()
        try:
            proof = self._object_insert_require(hwp, plan, control, where='before the readback', initial=True)
            started = time.monotonic()
            before_root = self._object_insert_readback(hwp, control, where='before insert')
            # Dry run of the after-edit proof (its cost, not its verdict) so an
            # edit whose proof cannot finish in time is refused before mutating.
            dry_run_proof(plan, before_root)
            slow = before_budget_reason(time.monotonic() - started)
            if slow:
                raise control.refuse('proof_budget', f'{OP} refused before mutation: {slow}')
            try:
                before = check_before(plan, before_root)
            except ObjectInsertError as exc:
                code = getattr(exc, 'code', 'unverifiable_layout')
                raise control.refuse(code if code in STEP_REFUSAL_CODES[OP] else 'unverifiable_layout', f'{OP} refused before mutation: {exc}') from exc
            proof = self._object_insert_require(hwp, plan, control, where='after before-readback')
            actions = self._object_insert_mutate(hwp, plan, control, mutation)
            try:
                after_root = self._object_insert_readback(hwp, control, where='after insert')
            except LocalCliRuntimeError as exc:
                exc.step_phase, exc.step_reason = 'readback', 'readback_after_failed'
                raise
            verification = evaluate_insert(plan, before_root, after_root)
            if not verification['ok']:
                error = LocalCliRuntimeError(f'{OP} refused to mark success: {"; ".join(verification["reasons"])}')
                error.step_phase, error.step_reason = 'verify', reason_code(verification['reasons'][0]) if verification['reasons'] else 'other'
                raise error
            return {
                'schema_version': SCHEMA_VERSION,
                'succeeded': True,
                'kind': plan['kind'],
                'plan': public_plan(plan),
                'pre_mutation_proof': {'pos': proof['pos'], 'selected_range': proof.get('selected_range'), 'snapshot': proof['snapshot']},
                'native_actions': actions,
                'before_controls': public_counts(before),
                'after_controls': public_counts(summarize(after_root)),
                'head_before': head_summary(before_root),
                'head_after': head_summary(after_root),
                'verification': verification,
                'undo': _undo_report(mutation),
                'next_proof_required': 'Render the page (page-screenshot or export-proof-range) and review it before saving; re-run where before another caret-bound edit.',
                'warnings': [],
            }
        except Exception as exc:
            if control.is_pending(exc):
                # Nothing before the mutator moves the caret or selection; register the exact refusal object.
                control.register(exc)
                raise
            if not mutation.started:
                raise
            message = str(exc) if isinstance(exc, LocalCliRuntimeError) else f'{OP} failed after native mutation started: {type(exc).__name__}: {exc}'
            rollback = dict(_ROLLBACK_HINT)
            rollback['undo'] = _undo_report(mutation)
            error = LocalCliMutationError(message, mutation_may_have_persisted=True, rollback=rollback)
            phase, reason = getattr(exc, 'step_phase', None), getattr(exc, 'step_reason', None)
            if type(phase) is str and type(reason) is str and phase in ('native', 'readback', 'verify'):
                error.step_phase, error.step_reason = phase, reason
            else:
                error.step_phase, error.step_reason = 'after_mutation', 'other'
            raise error from exc
