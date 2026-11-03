"""After the native insert of a shape or an equation, only an exact read of the editor state may lead to Cancel (FF1).

A drawing object (and possibly an equation) can stay selected after its Execute. The engine reads the editor state
strictly (caret, selection, selection mode). When that read fails or returns a malformed value, the state is unknown:
the step is held as a technical failure after the mutation, with nothing further on the SDK (no Cancel, no second
read, no GetTextFile, no proof, restore, save or close). Only a read that succeeds and shows a state that is not normal
leads to one Cancel, after which a second exact read must show the normal state; otherwise the step is held too.

Through the real public bundle and owning worker over the synthetic object and drawing SDKs. An exception class that
looks semantic (a refusal or a request error) raised by the SDK getter is still technical, and a reporting failure
after the hold does not release it. The normal paths keep their exact SDK sequence.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.object_insert import ObjectInsertError
from app.step_refusal import LocalCliStepRefusal
from tests.test_bundle_step_local_refusal import owner_for
from tests.test_deferred_step_intent import OBJECT_READS, _Checks, counted, modifies, run
from tests.test_object_insert_exact import _FakeHwp
from tests.test_shape_drawing import _DrawingHwp, shape_step

STATE_READS = ('get_pos', 'get_selected_pos', 'SelectionMode')
FAULTS = {
    'raises': lambda: OSError('synthetic getter failure'),
    'forged_refusal': lambda: LocalCliStepRefusal('forged', code='selection_required'),
    'forged_request_error': lambda: ObjectInsertError('forged', code='bookmark_exists'),
}
MALFORMED = {'get_selected_pos': (True, 0, 0), 'SelectionMode': True}


class _PostExecute:
    """Mixin for the synthetic SDKs: after the insert action, fault one state getter (``fault`` = (getter, how, nth))
    on its nth read, and log every SelectionMode read in ``sdk_calls`` like the other counted entry points."""

    INSERTS = ('DrawObjCreatorLine', 'DrawObjCreatorRectangle', 'DrawObjCreatorEllipse', 'EquationCreate')

    def arm(self, getter: str, how: str, nth: int = 1) -> None:
        self.fault, self.seen, self.armed = (getter, how, nth), 0, False

    def _maybe_fault(self, getter: str):
        fault = getattr(self, 'fault', None)
        if not getattr(self, 'armed', False) or fault is None or fault[0] != getter:
            return None
        self.seen += 1
        if self.seen != fault[2]:
            return None
        if fault[1] == 'malformed':
            return ('value', MALFORMED[getter])
        raise FAULTS[fault[1]]()

    def get_selected_pos(self):
        hit = self._maybe_fault('get_selected_pos')
        return hit[1] if hit else super().get_selected_pos()

    @property
    def SelectionMode(self):  # noqa: N802
        if hasattr(self, 'sdk_calls'):
            self.sdk_calls.append('SelectionMode')
        hit = self._maybe_fault('SelectionMode')
        return hit[1] if hit else self.mode_value()

    def after_insert(self, name: str) -> None:
        if name in self.INSERTS:
            self.armed = True


class _DrawingAfter(_PostExecute, _DrawingHwp):
    def mode_value(self):
        return self.selection_mode

    def native(self, name, items):
        result = super().native(name, items)
        self.after_insert(name)
        return result


class _EquationAfter(_PostExecute, _FakeHwp):
    """The object SDK fake; ``leaves_selected`` makes EquationCreate leave the new equation selected (mode 4), and
    ``cancel_fails`` makes Cancel return False and keep it selected."""

    def __init__(self, *, leaves_selected=False, cancel_fails=False):
        super().__init__()
        self.mode = 0
        self.leaves_selected, self.cancel_fails = leaves_selected, cancel_fails

    def mode_value(self):
        return self.mode

    def native(self, name, items):
        if name == 'Cancel':
            self.log.append('Cancel')
            if self.cancel_fails:
                return False
            self.mode = 0
            return True
        result = super().native(name, items)
        if name == 'EquationCreate' and self.leaves_selected:
            self.mode = 4
        self.after_insert(name)
        return result


def watched(hwp):
    hwp = counted(hwp, OBJECT_READS)
    modifies(hwp, 'native', lambda action, *rest: action not in hwp.noop and action != 'Cancel')
    hwp.fault_mark = None
    return hwp


def equation_step(hwp):
    return {'op': 'object_insert_exact', 'kind': 'equation', 'script': 'a over b', 'expected_pos': list(hwp.caret), 'confirm_mutation': True}


def after_insert_calls(hwp):
    """SDK calls after the (first) native insert call."""
    first = hwp.sdk_calls.index('native')
    return hwp.sdk_calls[first + 1:]


class UnreadableStateAfterTheInsert(_Checks):
    def assert_stopped_at(self, hwp, getter):
        tail = after_insert_calls(hwp)
        self.assertEqual(tail[-1], getter, tail)
        self.assertNotIn('native', tail)
        self.assertNotIn('GetTextFile', tail)
        self.assertNotIn('Cancel', hwp.log)

    def cases(self):
        for getter in ('get_selected_pos', 'SelectionMode'):
            for how in ('raises', 'malformed', 'forged_refusal', 'forged_request_error'):
                yield getter, how

    def test_a_shape_whose_state_cannot_be_read_after_execute_is_held_with_no_cancel(self):
        for name in ('rectangle', 'line_h'):
            for getter, how in self.cases():
                with self.subTest(name=name, getter=getter, how=how):
                    hwp = watched(_DrawingAfter())
                    hwp.arm(getter, how)
                    owner = owner_for(self, hwp)
                    result = run(owner, shape_step(hwp, name, True))
                    self.assertEqual(len(hwp.executed), 1)
                    self.assert_stopped_at(hwp, getter)
                    self.assert_held(owner, hwp, result, mutated=True, phase='native')
                    self.assertEqual(result['steps'][-1]['failure_diagnostic'].get('step_reason'), 'native_outcome_unknown')

    def test_an_equation_whose_state_cannot_be_read_after_execute_is_held_with_no_cancel(self):
        for getter, how in self.cases():
            with self.subTest(getter=getter, how=how):
                hwp = watched(_EquationAfter())
                hwp.arm(getter, how)
                owner = owner_for(self, hwp)
                result = run(owner, equation_step(hwp))
                self.assertIn('<EQUATION>', hwp.xml())
                self.assert_stopped_at(hwp, getter)
                self.assert_held(owner, hwp, result, mutated=True, phase='native')
                self.assertEqual(result['steps'][-1]['failure_diagnostic'].get('step_reason'), 'native_outcome_unknown')

    def test_a_failed_second_read_after_cancel_is_held_with_nothing_further(self):
        hwp = watched(_DrawingAfter())
        hwp.arm('SelectionMode', 'raises', nth=2)
        owner = owner_for(self, hwp)
        result = run(owner, shape_step(hwp, 'ellipse', False))
        tail = after_insert_calls(hwp)
        self.assertEqual(tail, ['get_pos', 'get_selected_pos', 'SelectionMode', 'native', 'get_pos', 'get_selected_pos', 'SelectionMode'])
        self.assertEqual(hwp.log.count('Cancel'), 1)
        self.assert_held(owner, hwp, result, mutated=True, phase='native')
        self.assertEqual(result['steps'][-1]['failure_diagnostic'].get('step_reason'), 'native_outcome_unknown')

    def test_a_reporting_failure_after_the_hold_does_not_release_it(self):
        for make in (lambda: watched(_DrawingAfter()), lambda: watched(_EquationAfter())):
            hwp = make()
            with self.subTest(sdk=type(hwp).__name__):
                hwp.arm('SelectionMode', 'raises')
                owner = owner_for(self, hwp)
                persist = owner.session._persist_command

                def fail(command):
                    if command.name == 'command-bundle' and command.state != 'queued':
                        raise OSError('synthetic terminal report failure')
                    return persist(command)
                step = equation_step(hwp) if isinstance(hwp, _EquationAfter) else shape_step(hwp, 'rectangle', True)
                with patch.object(owner.session, '_persist_command', fail):
                    with self.assertRaises(Exception):
                        run(owner, step)
                self.assertTrue(owner.session._native_custody_held)
                self.assertTrue(owner.command()['may_have_mutated'])
                self.assertNotIn('Cancel', hwp.log)
                self.assertEqual(after_insert_calls(hwp)[-1], 'SelectionMode')
                self.assertEqual(owner.cleanup_events(), [])


class KnownStateAfterTheInsert(_Checks):
    def test_a_selected_shape_is_released_by_cancel_with_the_same_sdk_sequence(self):
        hwp = watched(_DrawingAfter())
        owner = owner_for(self, hwp)
        result = run(owner, shape_step(hwp, 'rectangle', False))
        self.assertTrue(result['ok'], result)
        self.assertEqual(after_insert_calls(hwp)[:8], ['get_pos', 'get_selected_pos', 'SelectionMode', 'native',
                                                        'get_pos', 'get_selected_pos', 'SelectionMode', 'GetTextFile'])
        self.assertEqual(hwp.log.count('Cancel'), 1)

    def test_a_shape_still_selected_after_cancel_is_held(self):
        hwp = watched(_DrawingAfter('stays_selected'))
        owner = owner_for(self, hwp)
        result = run(owner, shape_step(hwp, 'line_v', True))
        self.assertNotIn('GetTextFile', after_insert_calls(hwp))
        self.assert_held(owner, hwp, result, mutated=True, phase='native')
        self.assertEqual(result['steps'][-1]['failure_diagnostic'].get('step_reason'), 'shape_not_released')

    def test_an_equation_in_normal_state_needs_no_cancel(self):
        hwp = watched(_EquationAfter())
        owner = owner_for(self, hwp)
        result = run(owner, equation_step(hwp))
        self.assertTrue(result['ok'], result)
        self.assertEqual(after_insert_calls(hwp)[:4], ['get_pos', 'get_selected_pos', 'SelectionMode', 'GetTextFile'])
        self.assertNotIn('Cancel', hwp.log)

    def test_a_selected_equation_is_released_by_cancel(self):
        hwp = watched(_EquationAfter(leaves_selected=True))
        owner = owner_for(self, hwp)
        result = run(owner, equation_step(hwp))
        self.assertTrue(result['ok'], result)
        self.assertEqual(after_insert_calls(hwp)[:8], ['get_pos', 'get_selected_pos', 'SelectionMode', 'native',
                                                        'get_pos', 'get_selected_pos', 'SelectionMode', 'GetTextFile'])

    def test_an_equation_cancel_that_fails_is_held_with_nothing_further(self):
        hwp = watched(_EquationAfter(leaves_selected=True, cancel_fails=True))
        owner = owner_for(self, hwp)
        result = run(owner, equation_step(hwp))
        self.assertEqual(after_insert_calls(hwp), ['get_pos', 'get_selected_pos', 'SelectionMode', 'native'])
        self.assert_held(owner, hwp, result, mutated=True, phase='native')
        self.assertEqual(result['steps'][-1]['failure_diagnostic'].get('step_reason'), 'close_return_failed')


if __name__ == '__main__':
    unittest.main()
