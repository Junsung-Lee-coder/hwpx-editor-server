"""Fixed phase diagnostics for the legacy table_split_exact and paragraph_delete_exact engines.

Their failures still hold the session (they publish mutation intent before invocation). Each failure now carries one
fixed phase in the public step's failure_diagnostic and the journal: the engine's own checks keep their class, and an
SDK or helper exception is wrapped with only its class name, so the cause class still tells the two apart. No
document text reaches the diagnostic. Through the real public bundle and owning worker over synthetic SDKs.
"""
from __future__ import annotations

import unittest

from tests.test_bundle_step_local_refusal import owner_for
from tests.test_guarded_bundle_observations import run
from tests.test_paragraph_replace_break import ParaDoc

DELETE = {'op': 'paragraph_delete_exact', 'match': '둘째 문단', 'expected_page': 1, 'confirm_remove': True,
          'expected_previous_contains': '첫째'}


def para_doc():
    hwp = ParaDoc(('첫째 문단', 'body'), ('둘째 문단', 'body'), ('셋째 문단', 'body'))
    hwp.current_page = lambda: 1
    hwp.get_text_file = lambda fmt='UNICODE', option='': '\r\n'.join(hwp.texts())
    return hwp


def failing_run(hwp, name, behave):
    plain = hwp.run

    def run_action(action):
        if action == name:
            return behave()
        return plain(action)
    hwp.run = run_action
    return hwp


def raise_os():
    raise OSError('synthetic SDK failure')


class ParagraphDeletePhases(unittest.TestCase):
    def owner(self, hwp):
        from app.local_cli_service import LocalCliService
        owner = owner_for(self, hwp)
        owner.service._find_text_on_expected_page = LocalCliService._find_text_on_expected_page.__get__(owner.service)
        return owner

    def test_success(self):
        hwp = para_doc()
        owner = self.owner(hwp)
        result = run(owner, DELETE)
        self.assertTrue(result['ok'], result)
        self.assertEqual(hwp.texts(), ['첫째 문단', '셋째 문단'])

    def test_each_phase_is_reported(self):
        def no_op_delete(hwp):
            return failing_run(hwp, 'Delete', lambda: True)

        def raising_find(hwp):
            def find(*a, **k):
                raise OSError('synthetic find failure')
            hwp.find = find
            return hwp
        def delete_unavailable(hwp):
            # Delete and Erase both raise, and the fake has no other delete method: the helper gives up.
            return failing_run(failing_run(hwp, 'Delete', raise_os), 'Erase', raise_os)

        def next_moves_to_page_two(hwp):
            plain = hwp.run

            def run_action(action):
                result = plain(action)
                if action == 'Delete':
                    hwp.current_page = lambda: 2
                return result
            hwp.run = run_action
            return hwp
        cases = (
            ('exact_line_proof', lambda h: h, dict(DELETE, match='둘째'), 'LocalCliRuntimeError', 'UNKNOWN'),
            ('find_target', raising_find, DELETE, 'LocalCliRuntimeError', 'UNKNOWN'),
            ('select', lambda h: failing_run(h, 'MoveSelNextParaBegin', raise_os), DELETE, 'LocalCliRuntimeError', 'UNKNOWN'),
            ('delete', delete_unavailable, DELETE, 'LocalCliRuntimeError', 'UNKNOWN'),
            ('post_count', no_op_delete, DELETE, 'LocalCliRuntimeError', 'UNKNOWN'),
            ('next_guard', next_moves_to_page_two, dict(DELETE, expected_next_contains='셋째'), 'LocalCliRuntimeError', 'UNKNOWN'),
        )
        for phase, arm, step, causal, cause in cases:
            with self.subTest(phase=phase):
                hwp = arm(para_doc())
                owner = self.owner(hwp)
                result = run(owner, step)
                self.assertFalse(result['ok'])
                status = result['steps'][-1]
                diagnostic = status['failure_diagnostic']
                self.assertEqual((diagnostic.get('step_phase'), diagnostic.get('step_reason')), (phase, 'failed'), diagnostic)
                self.assertEqual(diagnostic['causal_class'], causal, diagnostic)
                self.assertTrue(owner.session._native_custody_held)
                self.assertNotIn('둘째', str(diagnostic))
                self.assertNotIn('refusal_code', status)


class SplitTable:
    """A table the caret is in: TableLowerCell moves one row down; the split changes the page's table list."""

    def __init__(self, *, enter_ok=True, moves=True, split=lambda: {'succeeded': True}, changes=True):
        self.row, self.split_done, self.calls = 0, False, []
        self.enter_ok, self.moves, self.split, self.changes = enter_ok, moves, split, changes
        self.IsModified = False
        self.HAction = type('HAction', (), {'Run': staticmethod(self.run)})()

    def run(self, name):
        self.calls.append(name)
        if name == 'TableLowerCell' and self.moves:
            self.row += 1
        return True

    def get_pos(self):
        return (1, self.row, 0)

    def set_pos(self, *pos):
        return True

    def stub(self, service):
        service._bundle_resolve_control_target = lambda hwp, step, **kw: {
            'target_ctrl': object(), 'page_from': 1, 'page_to': 1, 'enumeration_mode': 'page_scope', 'section_anchor': None,
            'around': None, 'target_id': step['target_id'], 'expected_hash': step['expected_hash'], 'expected_page': 1,
            'before_item': {}, 'target_anchor_pos': None}
        service._bundle_table_cell_metrics = lambda hwp, ctrl: {}
        service._bundle_page_table_items = lambda hwp, **kw: [{'tables': 2 if (self.split_done and self.changes) else 1}]
        service._bundle_enter_table_cell_for_ctrl = lambda hwp, ctrl: {'is_cell': self.enter_ok, 'normal_edit_state': True}
        service._bundle_current_cell_addr_tuple = lambda hwp: (0, self.row)
        service._bundle_compact_snapshot = lambda hwp: {'is_cell': True, 'has_selection': False, 'selection_mode': 0}

        def split(hwp):
            result = self.split()
            self.split_done = bool(result.get('succeeded'))
            if self.split_done:
                self.IsModified = True
            return result
        service._run_table_split_action = split


class TableSplitPhases(unittest.TestCase):
    def run_split(self, table):
        from mcp_tests import parity_samples as samples
        owner = owner_for(self, table)
        table.stub(owner.service)
        return owner, run(owner, {'op': 'table_split_exact', **samples.BUNDLE_SAMPLES['table_split_exact'][0]})

    def test_success(self):
        owner, result = self.run_split(SplitTable())
        self.assertTrue(result['ok'], result)
        self.assertFalse(owner.session._native_custody_held)

    def test_each_phase_is_reported(self):
        def split_raises():
            raise OSError('synthetic TableSplitTable failure')
        for phase, table, cause in (
                ('pre_navigation', SplitTable(enter_ok=False), 'UNKNOWN'),
                ('navigation', SplitTable(moves=False), 'UNKNOWN'),
                ('split_action', SplitTable(split=lambda: {'succeeded': False, 'attempts': []}), 'UNKNOWN'),
                ('split_action', SplitTable(split=split_raises), 'UNKNOWN'),
                ('post_inventory', SplitTable(changes=False), 'UNKNOWN')):
            with self.subTest(phase=phase):
                owner, result = self.run_split(table)
                status = result['steps'][-1]
                diagnostic = status['failure_diagnostic']
                self.assertEqual((diagnostic.get('step_phase'), diagnostic.get('step_reason')), (phase, 'failed'), diagnostic)
                self.assertEqual(diagnostic['causal_class'], 'LocalCliRuntimeError')
                self.assertTrue(owner.session._native_custody_held)


if __name__ == '__main__':
    unittest.main()
