"""Exact single-object insertion at the caret for LocalCliService."""

from __future__ import annotations

from typing import Any

from app.edit_ops import _get_pos, _get_selected_pos, _get_selected_text
from app.local_cli_runtime import LocalCliRuntimeError, insert_text_at_caret
from app.local_cli_service_support import LocalCliMutationError
from app.object_insert import (
    CLOSE_ACTION,
    OP,
    SCHEMA_VERSION,
    SHAPE_KINDS,
    ObjectInsertError,
    check_before,
    evaluate_insert,
    native_items,
    normalize_step,
    parse_hwpml,
    public_counts,
    public_plan,
    summarize,
)

_ROLLBACK_HINT = {'attempted': False, 'hint': 'Inspect rendered proof; use undo or reopen the working copy before saving.'}


class _Mutation:
    """Tracks whether a native mutating action has been issued."""

    started = False


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

    def _object_insert_prepare_pset(self, hwp: Any, action_name: str, pset_name: str, items: dict[str, Any]) -> tuple[Any, Any]:
        """GetDefault + SetItem for an Execute action. Not mutating; raises LocalCliRuntimeError."""
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

    def _object_insert_readback(self, hwp: Any, *, where: str) -> Any:
        get_text = getattr(hwp, 'GetTextFile', None)
        if not callable(get_text):
            raise LocalCliRuntimeError(f'{OP} {where}: GetTextFile is unavailable, so the document cannot be read back')
        try:
            xml_text = get_text('HWPML2X', '')
        except Exception as exc:
            raise LocalCliRuntimeError(f'{OP} {where}: HWPML readback failed: {type(exc).__name__}: {exc}') from exc
        try:
            return parse_hwpml(xml_text)
        except ObjectInsertError as exc:
            raise LocalCliRuntimeError(f'{OP} {where}: {exc}') from exc

    def _object_insert_state(self, hwp: Any) -> dict[str, Any]:
        snapshot = self._bundle_compact_snapshot(hwp)
        mode = snapshot.get('selection_mode')
        mode_known = isinstance(mode, int) and not isinstance(mode, bool)
        has_selection = snapshot.get('has_selection')
        return {
            'snapshot': snapshot,
            'normal': mode_known and mode == 0 and has_selection is False,
            'selected': mode_known and has_selection is True,
        }

    def _object_insert_pos(self, hwp: Any) -> list[int] | None:
        try:
            pos = _get_pos(hwp)
            return [int(item) for item in pos[:3]] if len(pos) >= 3 else None
        except Exception:
            return None

    def _object_insert_require(self, hwp: Any, plan: dict[str, Any], *, where: str) -> dict[str, Any]:
        """Re-prove edit state, caret position and (hyperlink) the selected text. Raises on any doubt."""
        state = self._object_insert_state(hwp)
        policy = plan['selection_policy']
        if policy == 'none' and not state['normal']:
            raise LocalCliRuntimeError(f'{OP} {where}: editor is not provably in normal edit state: {state["snapshot"]!r}')
        if policy == 'required' and not state['selected']:
            raise LocalCliRuntimeError(f'{OP} {where}: {plan["kind"]} needs a live selection: {state["snapshot"]!r}')
        if policy == 'optional' and not (state['normal'] or state['selected']):
            raise LocalCliRuntimeError(f'{OP} {where}: editor state is not provably known: {state["snapshot"]!r}')
        pos = self._object_insert_pos(hwp)
        if pos != plan['expected_pos']:
            raise LocalCliRuntimeError(f'{OP} {where}: caret is at {pos!r}, expected {plan["expected_pos"]!r}; re-run where')
        proof: dict[str, Any] = {'snapshot': state['snapshot'], 'pos': pos}
        if plan['kind'] == 'hyperlink':
            try:
                range_before = list(_get_selected_pos(hwp))
                selected = _get_selected_text(hwp, keep_select=True)
                range_after = list(_get_selected_pos(hwp))
            except Exception as exc:
                raise LocalCliRuntimeError(f'{OP} {where}: selected text is unreadable: {type(exc).__name__}: {exc}') from exc
            if selected != plan['display_text']:
                raise LocalCliRuntimeError(f'{OP} {where}: selected text ({len(selected)} chars) is not exactly display_text ({len(plan["display_text"])} chars)')
            if range_before != range_after or not range_after or not range_after[0]:
                raise LocalCliRuntimeError(f'{OP} {where}: selection moved or vanished while its text was read')
            if not self._object_insert_state(hwp)['selected'] or self._object_insert_pos(hwp) != plan['expected_pos']:
                raise LocalCliRuntimeError(f'{OP} {where}: selection or caret changed while its text was read')
            proof['selected_range'] = range_after
        return proof

    def _object_insert_mutate(self, hwp: Any, plan: dict[str, Any], mutation: _Mutation) -> list[dict[str, Any]]:
        native = plan['native']
        actions: list[dict[str, Any]] = []
        if native['mode'] == 'run':
            self._object_insert_require(hwp, plan, where=f'before {native["action"]}')
            mutation.started = True
            result = self._object_insert_run(hwp, native['action'])
        else:
            execute, hset = self._object_insert_prepare_pset(hwp, native['action'], native['pset'], native_items(plan))
            self._object_insert_require(hwp, plan, where=f'before {native["action"]}')
            mutation.started = True
            result = self._object_insert_execute(execute, hset, native['action'])
        actions.append(result)
        if not result['succeeded']:
            raise LocalCliRuntimeError(f'{OP} native action did not succeed: {result!r}')
        if native.get('types_text'):
            try:
                insert_text_at_caret(hwp, plan['text'])
                actions.append({'action': 'InsertText', 'succeeded': True, 'chars': len(plan['text'])})
            except Exception as exc:
                actions.append({'action': 'InsertText', 'succeeded': False, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'})
                actions.append(self._object_insert_run(hwp, CLOSE_ACTION))
                raise LocalCliRuntimeError(f'{OP} typing into the new {plan["kind"]} failed: {actions!r}') from exc
        if native.get('closes'):
            closed = self._object_insert_run(hwp, CLOSE_ACTION)
            actions.append(closed)
            pos = self._object_insert_pos(hwp)
            if not closed['succeeded'] or pos is None or pos[0] != plan['expected_pos'][0]:
                raise LocalCliRuntimeError(f'{OP} could not return to the original list after editing the {plan["kind"]} (caret {pos!r}): {closed!r}')
        elif plan['kind'] in SHAPE_KINDS or plan['kind'] == 'equation':
            # A fresh drawing object may stay selected (or in drawing mode); Esc once.
            if not self._object_insert_state(hwp)['normal']:
                actions.append(self._object_insert_run(hwp, 'Cancel'))
        return actions

    def _bundle_object_insert_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = normalize_step(step)
        except ObjectInsertError as exc:
            raise LocalCliRuntimeError(f'{OP} refused: {exc}') from exc
        mutation = _Mutation()
        try:
            before_root = self._object_insert_readback(hwp, where='before insert')
            try:
                before = check_before(plan, before_root)
            except ObjectInsertError as exc:
                raise LocalCliRuntimeError(f'{OP} refused before mutation: {exc}') from exc
            proof = self._object_insert_require(hwp, plan, where='after before-readback')
            actions = self._object_insert_mutate(hwp, plan, mutation)
            after_root = self._object_insert_readback(hwp, where='after insert')
            verification = evaluate_insert(plan, before_root, after_root)
            if not verification['ok']:
                raise LocalCliRuntimeError(f'{OP} refused to mark success: {"; ".join(verification["reasons"])}')
            return {
                'schema_version': SCHEMA_VERSION,
                'succeeded': True,
                'kind': plan['kind'],
                'plan': public_plan(plan),
                'pre_mutation_proof': {'pos': proof['pos'], 'selected_range': proof.get('selected_range'), 'snapshot': proof['snapshot']},
                'native_actions': actions,
                'before_controls': public_counts(before),
                'after_controls': public_counts(summarize(after_root)),
                'verification': verification,
                'next_proof_required': 'Render the page (page-screenshot or export-proof-range) and review it before saving; re-run where before another caret-bound edit.',
                'warnings': [],
            }
        except Exception as exc:
            if not mutation.started:
                raise
            message = str(exc) if isinstance(exc, LocalCliRuntimeError) else f'{OP} failed after native mutation started: {type(exc).__name__}: {exc}'
            raise LocalCliMutationError(message, mutation_may_have_persisted=True, rollback=dict(_ROLLBACK_HINT)) from exc
