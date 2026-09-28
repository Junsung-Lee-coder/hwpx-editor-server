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
    parse_table_grid_xml,
)


class LocalCliTableStructureMixin:
    """Exact table-structure edits for LocalCliService.

    Every edit resolves the table by inventory proof, reads the grid through
    HWPML before and after, and only reports success when the grid changed
    exactly as planned. Any failure after the first native action is raised
    as LocalCliMutationError with mutation_may_have_persisted=True.
    """

    def _table_structure_run_action(self, hwp: Any, action_name: str) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        haction_run = getattr(getattr(hwp, 'HAction', None), 'Run', None)
        run = getattr(hwp, 'Run', None)
        for label, runner in ((f'HAction.Run({action_name})', haction_run), (f'Run({action_name})', run)):
            if not callable(runner):
                continue
            try:
                raw = runner(action_name)
            except Exception as exc:
                attempts.append({'method': label, 'error': f'{type(exc).__name__}: {exc}'})
                continue
            attempts.append({'method': label, 'result': bool(raw) if raw is not None else None})
            return {'succeeded': raw is None or bool(raw), 'attempts': attempts}
        return {'succeeded': False, 'attempts': attempts}

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

    def _table_structure_clear_selection(self, hwp: Any) -> dict[str, Any]:
        snapshot = self._bundle_compact_snapshot(hwp)
        if snapshot.get('has_selection') or int(snapshot.get('selection_mode') or 0) != 0:
            self._table_structure_run_action(hwp, 'Cancel')
            snapshot = self._bundle_compact_snapshot(hwp)
        return snapshot

    def _table_structure_grid(self, hwp: Any) -> dict[str, Any]:
        """Read the enclosing table's grid through HWPML; caret must be in a cell."""
        select_front = getattr(hwp, 'SelectCtrlFront', None)
        get_text = getattr(hwp, 'GetTextFile', None)
        if not callable(select_front) or not callable(get_text):
            raise LocalCliRuntimeError(f'{OP} needs SelectCtrlFront and GetTextFile for table grid readback')
        pos = _get_pos(hwp)
        try:
            select_front()
            xml_text = get_text('HWPML2X', 'saveblock')
        finally:
            if pos is not None and len(pos) >= 3:
                _set_pos(hwp, int(pos[0]), int(pos[1]), int(pos[2]))
        snapshot = self._table_structure_clear_selection(hwp)
        if snapshot.get('has_selection') or int(snapshot.get('selection_mode') or 0) != 0:
            raise LocalCliRuntimeError(f'{OP} could not return to normal edit state after grid readback')
        try:
            return parse_table_grid_xml(xml_text)
        except TableStructureError as exc:
            raise LocalCliRuntimeError(f'{OP} grid readback failed: {exc}') from exc

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

    def _table_structure_mutate(self, hwp: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
        action = plan['action']
        actions: list[dict[str, Any]] = []
        if action in ROW_COL_ACTIONS:
            for index in range(plan['count']):
                result = self._table_structure_run_action(hwp, plan['hancom_action'])
                actions.append({'repeat': index + 1, 'action': plan['hancom_action'], **result})
                if not result['succeeded']:
                    break
            return actions
        if action == 'merge_cells':
            sequence = ['TableCellBlock', 'TableCellBlockExtend']
            sequence += ['TableRightCell'] * (plan['end_col'] - plan['col'])
            sequence += ['TableLowerCell'] * (plan['end_row'] - plan['row'])
            sequence += ['TableMergeCell']
            for action_name in sequence:
                result = self._table_structure_run_action(hwp, action_name)
                actions.append({'action': action_name, **result})
                if not result['succeeded']:
                    break
            actions.append({'action': 'Cancel', **self._table_structure_run_action(hwp, 'Cancel')})
            return actions
        # split_cell: Rows/Cols of 0 leave that dimension unsplit.
        rows = plan['split_rows'] if plan['split_rows'] > 1 else 0
        cols = plan['split_cols'] if plan['split_cols'] > 1 else 0
        entry: dict[str, Any] = {'action': 'TableSplitCell', 'rows': rows, 'cols': cols}
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
            entry.update({'succeeded': False, 'error': f'{type(exc).__name__}: {exc}'})
        actions.append(entry)
        return actions

    def _bundle_table_structure_exact(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        resolved = self._bundle_resolve_control_target(hwp, step, op_name=OP, require_table=True)
        original_pos = None
        try:
            original_pos = _get_pos(hwp)
        except Exception:
            original_pos = None
        mutation_started = False
        try:
            enter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
            if not enter.get('is_cell') or not enter.get('normal_edit_state'):
                raise LocalCliRuntimeError(f'{OP} cannot enter the target table in normal edit state')
            before = self._table_structure_grid(hwp)
            try:
                plan = check_plan(step, before)
            except TableStructureError as exc:
                raise LocalCliRuntimeError(f'{OP} refused before mutation: {exc}') from exc
            goto = self._table_structure_goto(hwp, plan['row'], plan['col'])
            if not goto['reached']:
                raise LocalCliRuntimeError(f'{OP} could not place the caret on {plan["address"]}: {goto["attempts"]!r}')
            before_snapshot = self._table_structure_clear_selection(hwp)
            mutation_started = True
            actions = self._table_structure_mutate(hwp, plan)
            if not all(item.get('succeeded', True) for item in actions if item.get('action') != 'Cancel'):
                raise LocalCliRuntimeError(f'{OP} native action did not succeed: {actions!r}')
            if not self._bundle_compact_snapshot(hwp).get('is_cell'):
                reenter = self._bundle_enter_table_cell_for_ctrl(hwp, resolved['target_ctrl'])
                if not reenter.get('is_cell'):
                    raise LocalCliRuntimeError(f'{OP} cannot re-enter the table for post-edit readback')
            after = self._table_structure_grid(hwp)
            verification = evaluate_change(plan, before, after)
            if not verification['ok']:
                raise LocalCliRuntimeError(f'{OP} refused to mark success: {"; ".join(verification["reasons"])}')
            return {
                'schema_version': SCHEMA_VERSION,
                'succeeded': True,
                'action': plan['action'],
                'plan': plan,
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
                'enter': enter,
                'goto': goto,
                'before_snapshot': before_snapshot,
                'before_grid': before,
                'native_actions': actions,
                'after_grid': after,
                'verification': verification,
                'next_proof_required': 'Render the page (page-screenshot or export-proof-range) and review it before saving; the target proof_hash changes after this edit, so re-inventory before another exact edit.',
                'warnings': [],
            }
        except LocalCliRuntimeError as exc:
            if not mutation_started:
                raise
            raise LocalCliMutationError(
                str(exc),
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'hint': 'Inspect rendered proof; use undo or reopen the working copy before saving.'},
            ) from exc
        except Exception as exc:
            if not mutation_started:
                raise
            raise LocalCliMutationError(
                f'{OP} failed after native mutation started: {type(exc).__name__}: {exc}',
                mutation_may_have_persisted=True,
                rollback={'attempted': False, 'hint': 'Inspect rendered proof; use undo or reopen the working copy before saving.'},
            ) from exc
        finally:
            if original_pos is not None and len(original_pos) >= 3:
                try:
                    _set_pos(hwp, int(original_pos[0]), int(original_pos[1]), int(original_pos[2]))
                except Exception:
                    pass
