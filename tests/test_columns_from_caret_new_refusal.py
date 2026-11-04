"""columns apply_to from_caret_new is refused from the request alone, before any SDK call of the operation.

On Hancom 2024 13.0.0.3457, MultiColumn with ApplyTo 6 rewrote the section's existing column definition (count 1 to 2)
instead of adding one at the caret, at the first and at an interior paragraph (owned characterization), and the engine
then held the changed working copy. The request is refused with the fixed code columns_from_caret_new_unsupported: the
operation makes no SDK call at all (no caret read, no parameter set, no readback, no MultiColumn), the session is not held,
its dirty state and close debt stay as they were, and current_section columns still work and save afterwards. The
capability row is unsupported with that code and nothing to undo, and the MCP schema keeps the value only to reach that
refusal. Through the real public bundle, registered command package and owning worker over the synthetic layout SDK.
Not Hancom evidence.
"""
from __future__ import annotations

import unittest

from app import layout_ops
from app.step_refusal import STEP_REFUSAL_CODES
from hwpx_mcp import parity
from hwpx_mcp.operations import LayoutStep
from tests.test_bundle_step_local_refusal import owner_for
from tests.test_deferred_step_intent import LAYOUT_READS, _Checks, inside, layout_doc
from tests.test_guarded_bundle_observations import run

CODE = 'columns_from_caret_new_unsupported'
STEP = {'op': 'layout_exact', 'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'gap_mm': 8.0, 'apply_to': 'from_caret_new'}
CURRENT = {**STEP, 'apply_to': 'current_section'}
OPERATION = ('/app/local_cli_layout.py', '/app/command_packages/commands/layout_exact/run.py', '/app/layout_ops.py')


class _Spy:
    """While armed: any SDK entry point reached from the operation's own code, and any action from anywhere, is recorded
    and raises, so a refused request that touches the SDK can never pass as clean."""

    def __init__(self, hwp):
        self.hwp, self.armed, self.violations, self.envelope = hwp, False, [], []
        for name in (*LAYOUT_READS, 'get_text_file'):
            method = getattr(hwp, name, None)
            if callable(method):
                setattr(hwp, name, self._wrap(name, method))
        hwp.HAction = self._proxy('HAction', hwp.HAction)
        hwp.HParameterSet = self._proxy('HParameterSet', hwp.HParameterSet)
        spy = self

        class Spied(type(hwp)):
            @property
            def SelectionMode(self):  # noqa: N802
                spy.guard('SelectionMode')
                return super().SelectionMode
        hwp.__class__ = Spied

    def guard(self, name):
        if not self.armed:
            return
        if any(inside(path) for path in OPERATION) or name in ('execute', 'run', 'HAction.Execute', 'HAction.Run'):
            self.violations.append(name)
            raise AssertionError(f'native call {name} from the refused operation')
        self.envelope.append(name)

    def _wrap(self, name, method):
        def wrapper(*args, **kwargs):
            self.guard(name)
            return method(*args, **kwargs)
        return wrapper

    def _proxy(self, label, target):
        spy = self

        class Proxy:
            def __getattr__(self, attr):
                spy.guard(f'{label}.{attr}')
                return getattr(target, attr)
        return Proxy()


class ColumnsFromCaretNewRefusal(_Checks):
    def test_refused_before_any_sdk_call_then_current_section_columns_still_work(self):
        for prior_dirty in (False, True):
            with self.subTest(prior_dirty=prior_dirty):
                hwp = layout_doc()
                owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                spy = _Spy(hwp)
                coldefs = [list(map(dict, section['coldefs'])) for section in hwp.sections]
                spy.armed = True
                result = run(owner, STEP)
                spy.armed = False
                self.assertEqual(spy.violations, [], spy.violations)
                self.assert_clean_refusal(owner, result, 0, CODE, dirty=prior_dirty)
                self.assertEqual((hwp.log, hwp.executed_apply_to, hwp.mutations()), ([], [], []))
                self.assertEqual([section['coldefs'] for section in hwp.sections], coldefs)
                self.assertEqual(hwp.caret, (0, 0, 0))
                self.assertTrue(owner.session._thread.is_alive())
                self.assert_continues_and_saves(owner, CURRENT)
                self.assertEqual(hwp.executed_apply_to, [2])
                self.assertEqual([coldef['Count'] for coldef in hwp.sections[0]['coldefs']], [2])

    def test_the_capability_row_says_unsupported_with_the_code_and_nothing_to_undo(self):
        self.assertIn(CODE, STEP_REFUSAL_CODES['layout_exact'])
        row = {item['variant']: item for item in parity.VARIANTS['layout_exact']}['columns:from_caret_new']
        self.assertEqual((row['native'], row.get('refusal_code'), row.get('undo')), ('unsupported', CODE, 'not_applicable'))
        self.assertNotIn('columns:from_caret_new', layout_ops.UNDO_STEPS)

    def test_the_planner_never_plans_apply_to_6(self):
        self.assertNotIn(6, layout_ops.COLUMN_APPLY_TO.values())
        request = layout_ops.normalize_request(STEP)
        with self.assertRaises(layout_ops.LayoutError) as caught:
            layout_ops.plan_columns(request, {'Count': 1, 'SameSize': 1, 'SameGap': 0}, {'section_count': 1, 'coldefs_by_section': [[]], 'coldefs': []})
        self.assertEqual(getattr(caught.exception, 'code', None), CODE)

    def test_the_mcp_schema_keeps_the_value_only_to_reach_the_refusal(self):
        self.assertEqual(LayoutStep.model_validate(STEP).apply_to, 'from_caret_new')
        description = LayoutStep.model_fields['apply_to'].description
        self.assertIn(CODE, description)
        self.assertIn(CODE, LayoutStep.__doc__)
        self.assertNotIn('columns: current_section or from_caret_new', description)


if __name__ == '__main__':
    unittest.main()
