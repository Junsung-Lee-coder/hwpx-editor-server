"""Exact table-structure edits (rows, columns, merge, split) for LocalCliService."""

from __future__ import annotations

from typing import Any

from app.edit_ops import _get_pos, _set_pos
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from app.table_structure import (
    OP,
    ROW_COL_ACTIONS,
    SCHEMA_VERSION,
    TableStructureError,
    cell_address,
    check_plan,
    evaluate_change,
    parse_cell_address,
    parse_cell_range,
    parse_table_grid_xml,
    public_grid,
)

_ROLLBACK_HINT = {'attempted': False, 'hint': 'Close the working copy without saving and reopen it; a single Undo does not guarantee a full rollback of this edit.'}


class _Mutation:
    """Tracks whether a native mutating action has been issued, and how many."""

    started = False
    native_edits = 0


def _undo_report(mutation: _Mutation, *, succeeded: bool) -> dict[str, Any]:
    """What `hwpx undo` will do after this edit (_BUNDLE_UNDO_POLICY): one step only for one successful native edit."""
    one_step = succeeded and mutation.native_edits == 1
    return {
        'native_editing_actions': mutation.native_edits,
        'hwpx_undo': 'one_step' if one_step else 'refused',
        'undo_units_verified': False,
    }


class LocalCliTableStructureMixin:
    """Exact table-structure edits for LocalCliService.

    Every native action runs only after three facts are re-proven at that
    moment: the caret's enclosing table is the resolved target (CtrlInstID),
    the caret is on the planned cell, and the editor is in normal edit state
    with no selection (an unreadable state counts as not normal). The grid is
    read through HWPML before and after and must change exactly as planned.
    Any failure once a mutating action was issued is raised as
    LocalCliMutationError with mutation_may_have_persisted=True.
    """

    def _table_structure_run_action(self, hwp: Any, action_name: str) -> dict[str, Any]:
        """Run one Hancom action exactly once.

        hwp.Run is used only when HAction.Run does not exist. An action that
        raises is never retried through another entry point: its native
        outcome is unknown, and a second attempt could apply it twice.
        """
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

    def _table_structure_address(self, hwp: Any) -> tuple[int, int] | None:
        # Use the "A1" string form: pyhwpx's tuple form is (row, col) while
        # older helpers here assume (col, row), so the string is unambiguous.
        getter = getattr(hwp, 'get_cell_addr', None)
        if not callable(getter):
            return None
        for kwargs in ({'as_': 'str'}, {}):
            try:
                parsed = parse_cell_address(getter(**kwargs))
            except Exception:
                continue
            if parsed is not None:
                return parsed
        return None

    def _table_structure_inst_id(self, ctrl: Any) -> str | None:
        if ctrl is None:
            return None
        value = self._bundle_control_scalar(ctrl, 'CtrlInstID')
        return str(value) if value not in (None, '') else None

    def _table_structure_parent_inst_id(self, hwp: Any) -> str | None:
        try:
            parent = getattr(hwp, 'ParentCtrl', None)
        except Exception:
            return None
        return self._table_structure_inst_id(parent)

    def _table_structure_state(self, hwp: Any) -> dict[str, Any]:
        """Strict normal-edit check: every field must be positively known."""
        snapshot = self._bundle_compact_snapshot(hwp)
        mode = snapshot.get('selection_mode')
        normal = (
            snapshot.get('is_cell') is True
            and snapshot.get('has_selection') is False
            and isinstance(mode, int)
            and not isinstance(mode, bool)
            and mode == 0
        )
        return {'normal': normal, 'snapshot': snapshot}

    def _table_structure_clear_selection(self, hwp: Any) -> dict[str, Any]:
        state = self._table_structure_state(hwp)
        if not state['normal'] and state['snapshot'].get('has_selection') is not False:
            self._table_structure_run_action(hwp, 'Cancel')
            state = self._table_structure_state(hwp)
        return state

    def _table_structure_require(self, hwp: Any, target_inst: str, *, where: str, address: tuple[int, int] | None = None) -> dict[str, Any]:
        """Re-prove target table, caret cell and normal edit state; raise if any is not proven."""
        parent_inst = self._table_structure_parent_inst_id(hwp)
        if parent_inst != target_inst:
            raise LocalCliRuntimeError(
                f'{OP} {where}: caret is not inside the target table (ParentCtrl CtrlInstID {parent_inst!r}, target {target_inst!r})'
            )
        state = self._table_structure_state(hwp)
        if not state['normal']:
            raise LocalCliRuntimeError(f'{OP} {where}: editor is not provably in normal edit state: {state["snapshot"]!r}')
        if address is not None:
            here = self._table_structure_address(hwp)
            if here != address:
                raise LocalCliRuntimeError(f'{OP} {where}: caret is on {here!r}, expected {cell_address(*address)}')
        return state['snapshot']

    def _table_structure_selected_inst_id(self, hwp: Any) -> str | None:
        try:
            selected = getattr(hwp, 'CurSelectedCtrl', None)
        except Exception:
            return None
        return self._table_structure_inst_id(selected)

    def _table_structure_select_table(self, hwp: Any, target_inst: str, pos: Any, *, where: str) -> dict[str, Any]:
        """Select exactly the target table as a control and prove it via CurSelectedCtrl.

        SelectCtrl(CtrlInstID) is tried first: SelectCtrlFront selects the
        control in front of the caret, which is an embedded picture (not the
        table) when the caret sits at the start of a cell that begins with one.
        SelectCtrlFront is only a fallback, and either way the selection is
        accepted only when CurSelectedCtrl reports the target CtrlInstID.
        """
        attempts: list[dict[str, Any]] = []
        select_ctrl = getattr(hwp, 'SelectCtrl', None)
        if callable(select_ctrl):
            for args in ((target_inst, 1), (target_inst,)):
                try:
                    raw = select_ctrl(*args)
                except Exception as exc:
                    attempts.append({'method': 'SelectCtrl', 'args': list(args), 'error': f'{type(exc).__name__}: {exc}'})
                    continue
                selected = self._table_structure_selected_inst_id(hwp)
                attempts.append({'method': 'SelectCtrl', 'args': list(args), 'result': raw, 'selected_ctrl_inst_id': selected})
                if selected == target_inst:
                    return {'method': 'SelectCtrl(CtrlInstID)', 'attempts': attempts}
        select_front = getattr(hwp, 'SelectCtrlFront', None)
        if callable(select_front):
            if pos is not None and len(pos) >= 3:
                _set_pos(hwp, int(pos[0]), int(pos[1]), int(pos[2]))
            try:
                raw = select_front()
                selected = self._table_structure_selected_inst_id(hwp)
                attempts.append({'method': 'SelectCtrlFront', 'result': raw, 'selected_ctrl_inst_id': selected})
                if selected == target_inst:
                    return {'method': 'SelectCtrlFront', 'attempts': attempts}
            except Exception as exc:
                attempts.append({'method': 'SelectCtrlFront', 'error': f'{type(exc).__name__}: {exc}'})
        raise LocalCliRuntimeError(
            f'{OP} {where}: could not select the target table {target_inst!r} for grid readback '
            f'(CurSelectedCtrl never reported it): {attempts!r}'
        )

    def _table_structure_grid(self, hwp: Any, target_inst: str, *, where: str) -> dict[str, Any]:
        """Read the target table's grid through HWPML; the caret must be inside that table."""
        get_text = getattr(hwp, 'GetTextFile', None)
        if not callable(get_text):
            raise LocalCliRuntimeError(f'{OP} needs GetTextFile for table grid readback')
        parent_inst = self._table_structure_parent_inst_id(hwp)
        if parent_inst != target_inst:
            raise LocalCliRuntimeError(
                f'{OP} {where}: grid readback is not bound to the target table (ParentCtrl CtrlInstID {parent_inst!r}, target {target_inst!r})'
            )
        pos = _get_pos(hwp)
        try:
            self._table_structure_select_table(hwp, target_inst, pos, where=where)
            xml_text = get_text('HWPML2X', 'saveblock')
        finally:
            if pos is not None and len(pos) >= 3:
                _set_pos(hwp, int(pos[0]), int(pos[1]), int(pos[2]))
        if not self._table_structure_clear_selection(hwp)['normal']:
            raise LocalCliRuntimeError(f'{OP} {where}: could not return to normal edit state after grid readback')
        try:
            return parse_table_grid_xml(xml_text)
        except TableStructureError as exc:
            raise LocalCliRuntimeError(f'{OP} {where}: grid readback failed: {exc}') from exc

    def _table_structure_goto(self, hwp: Any, row: int, col: int) -> dict[str, Any]:
        target = (row, col)
        address = cell_address(row, col)
        attempts: list[dict[str, Any]] = []
        goto = getattr(hwp, 'goto_addr', None)
        if callable(goto):
            try:
                attempts.append({'method': f'goto_addr({address})', 'result': goto(address)})
            except Exception as exc:
                attempts.append({'method': f'goto_addr({address})', 'error': f'{type(exc).__name__}: {exc}'})
            if self._table_structure_address(hwp) == target:
                return {'reached': True, 'address': address, 'attempts': attempts}
        # Fallback: from A1, step down then right, checking the address each time.
        for action_name in ('TableColBegin', 'TableColPageUp'):
            attempts.append({'method': action_name, **self._table_structure_run_action(hwp, action_name)})
        current = self._table_structure_address(hwp)
        attempts.append({'method': 'address-after-home', 'address': current})
        for axis, action_name in ((0, 'TableLowerCell'), (1, 'TableRightCell')):
            while current is not None and current[axis] < target[axis]:
                self._table_structure_run_action(hwp, action_name)
                moved = self._table_structure_address(hwp)
                attempts.append({'method': action_name, 'address': moved})
                if moved is None or moved[axis] <= current[axis]:
                    break
                current = moved
        reached = self._table_structure_address(hwp) == target
        return {'reached': reached, 'address': address, 'attempts': attempts}

    def _table_structure_goto_and_require(self, hwp: Any, target_inst: str, plan: dict[str, Any], *, where: str) -> dict[str, Any]:
        goto = self._table_structure_goto(hwp, plan['row'], plan['col'])
        if not goto['reached']:
            raise LocalCliRuntimeError(f'{OP} {where}: could not place the caret on {plan["address"]}: {goto["attempts"]!r}')
        self._table_structure_clear_selection(hwp)
        goto['snapshot'] = self._table_structure_require(hwp, target_inst, where=where, address=(plan['row'], plan['col']))
        return goto

    def _table_structure_native(self, hwp: Any, action_name: str, mutation: _Mutation) -> dict[str, Any]:
        mutation.started = True
        mutation.native_edits += 1
        return {'action': action_name, **self._table_structure_run_action(hwp, action_name)}

    def _table_structure_mutate(self, hwp: Any, plan: dict[str, Any], target_inst: str, mutation: _Mutation) -> list[dict[str, Any]]:
        action = plan['action']
        actions: list[dict[str, Any]] = []
        if action in ROW_COL_ACTIONS:
            for index in range(plan['count']):
                where = f'before {plan["hancom_action"]} #{index + 1}'
                if index:
                    # Every repeat starts from the target cell again, so a caret
                    # that Hancom moved elsewhere cannot hit the wrong line.
                    self._table_structure_goto_and_require(hwp, target_inst, plan, where=where)
                else:
                    self._table_structure_require(hwp, target_inst, where=where, address=(plan['row'], plan['col']))
                result = self._table_structure_native(hwp, plan['hancom_action'], mutation)
                actions.append({'repeat': index + 1, **result})
                if not result['succeeded']:
                    break
            return actions
        if action == 'merge_cells':
            self._table_structure_require(hwp, target_inst, where='before merge selection', address=(plan['row'], plan['col']))
            selection_steps = ['TableCellBlock', 'TableCellBlockExtend']
            selection_steps += ['TableRightCell'] * (plan['end_col'] - plan['col'])
            selection_steps += ['TableLowerCell'] * (plan['end_row'] - plan['row'])
            for action_name in selection_steps:
                result = self._table_structure_run_action(hwp, action_name)
                actions.append({'action': action_name, **result})
                if not result['succeeded']:
                    self._table_structure_run_action(hwp, 'Cancel')
                    raise LocalCliRuntimeError(f'{OP} merge selection step {action_name} did not succeed: {result!r}')
            selected = self._table_structure_selected_range(hwp)
            actions.append({'action': 'get_selected_range', **selected})
            parent_inst = self._table_structure_parent_inst_id(hwp)
            if selected['cells'] != plan['selection'] or parent_inst != target_inst:
                self._table_structure_run_action(hwp, 'Cancel')
                raise LocalCliRuntimeError(
                    f'{OP} refused before TableMergeCell: selected range {selected!r} in table {parent_inst!r} '
                    f'is not exactly {plan["address"]}:{plan["end_address"]} in the target table'
                )
            result = self._table_structure_native(hwp, 'TableMergeCell', mutation)
            actions.append(result)
            actions.append({'action': 'Cancel', **self._table_structure_run_action(hwp, 'Cancel')})
            return actions
        # split_cell: Rows/Cols of 0 leave that dimension unsplit.
        self._table_structure_require(hwp, target_inst, where='before TableSplitCell', address=(plan['row'], plan['col']))
        rows = plan['split_rows'] if plan['split_rows'] > 1 else 0
        cols = plan['split_cols'] if plan['split_cols'] > 1 else 0
        entry: dict[str, Any] = {'action': 'TableSplitCell', 'rows': rows, 'cols': cols}
        mutation.started = True
        try:
            wrapper = getattr(hwp, 'TableSplitCell', None)
            if callable(wrapper):
                raw = wrapper(Rows=rows, Cols=cols, DistributeHeight=0, Merge=0)
                entry['method'] = 'TableSplitCell(Rows, Cols)'
            else:
                pset = hwp.HParameterSet.HTableSplitCell
                hwp.HAction.GetDefault('TableSplitCell', pset.HSet)
                pset.Rows, pset.Cols, pset.DistributeHeight, pset.Merge = rows, cols, 0, 0
                raw = hwp.HAction.Execute('TableSplitCell', pset.HSet)
                entry['method'] = 'HAction.Execute(TableSplitCell)'
            entry['succeeded'] = raw is None or bool(raw)
        except Exception as exc:
            entry.update({'succeeded': False, 'outcome_unknown': True, 'error': f'{type(exc).__name__}: {exc}'})
        actions.append(entry)
        return actions

    def _table_structure_selected_range(self, hwp: Any) -> dict[str, Any]:
        getter = getattr(hwp, 'get_selected_range', None)
        if not callable(getter):
            return {'cells': None, 'error': 'get_selected_range unavailable'}
        try:
            raw = getter()
        except Exception as exc:
            return {'cells': None, 'error': f'{type(exc).__name__}: {exc}'}
        cells = parse_cell_range(raw)
        return {'cells': None if cells is None else sorted(cells), 'raw': raw if isinstance(raw, (list, str)) else repr(raw)}

    def _bundle_table_structure_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name=OP, require_table=True)
        target_inst = self._table_structure_inst_id(resolved['target_ctrl'])
        if target_inst is None:
            raise LocalCliRuntimeError(f'{OP} refused: the target table exposes no CtrlInstID, so edits cannot be bound to it')
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        mutation = _Mutation()
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
            if not enter.get('is_cell') or not enter.get('normal_edit_state'):
                raise LocalCliRuntimeError(f'{OP} cannot enter the target table in normal edit state')
            before = self._table_structure_grid(hwp, target_inst, where='before edit')
            try:
                plan = check_plan(step, before)
            except TableStructureError as exc:
                raise LocalCliRuntimeError(f'{OP} refused before mutation: {exc}') from exc
            goto = self._table_structure_goto_and_require(hwp, target_inst, plan, where='before mutation')
            actions = self._table_structure_mutate(hwp, plan, target_inst, mutation)
            failed = [item for item in actions if item.get('succeeded') is False and item.get('action') != 'Cancel']
            if failed:
                raise LocalCliRuntimeError(f'{OP} native action did not succeed: {failed!r}')
            if self._table_structure_parent_inst_id(hwp) != target_inst:
                reenter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
                if not reenter.get('is_cell'):
                    raise LocalCliRuntimeError(f'{OP} cannot re-enter the target table for post-edit readback')
            after = self._table_structure_grid(hwp, target_inst, where='after edit')
            verification = evaluate_change(plan, before, after)
            if not verification['ok']:
                raise LocalCliRuntimeError(f'{OP} refused to mark success: {"; ".join(verification["reasons"])}')
            public_plan = {key: value for key, value in plan.items() if key != 'selection'}
            return {
                'schema_version': SCHEMA_VERSION,
                'succeeded': True,
                'action': plan['action'],
                'plan': public_plan,
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
                    'ctrl_inst_id': target_inst,
                    'matched_before': resolved['before_item'],
                    'target_anchor_pos': list(resolved['target_anchor_pos']) if resolved['target_anchor_pos'] is not None else None,
                },
                'enter': enter,
                'goto': goto,
                'before_grid': public_grid(before),
                'native_actions': actions,
                'undo': _undo_report(mutation, succeeded=True),
                'after_grid': public_grid(after),
                'verification': verification,
                'next_proof_required': 'Render the page (page-screenshot or export-proof-range) and review it before saving; the target proof_hash changes after this edit, so re-inventory before another exact edit.',
                'warnings': [],
            }
        except Exception as exc:
            if not mutation.started:
                raise
            message = str(exc) if isinstance(exc, LocalCliRuntimeError) else f'{OP} failed after native mutation started: {type(exc).__name__}: {exc}'
            rollback = dict(_ROLLBACK_HINT)
            rollback['undo'] = _undo_report(mutation, succeeded=False)
            raise LocalCliMutationError(message, mutation_may_have_persisted=True, rollback=rollback) from exc
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
