"""Uncertainty propagation for official numbering after a mutating native call has been reached.

Synthetic fakes only. They prove call ordering and error classification of this source; they are NOT
Windows/Hancom evidence and say nothing about native behaviour.

Once a mutating ``Execute`` was attempted, any later ordinary failure (readback, census, preservation
delta, or the call itself) leaves the document state unproven; a termination signal must not be
swallowed and must not trigger caret navigation, retry, Undo, Close, Clear, Quit or discard.
"""
from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app import native_numbering as nn
from app.local_cli_bundle_numbering import LocalCliBundleNumberingMixin
from app.local_cli_service_support import LocalCliMutationError
from tests.native_numbering_fakes import FakeHwp
from tests.test_native_numbering_review_contract import NUMBERING_STEP, _hwpml, doc, run_bundle

NO_CLEANUP = {'attempted': False, 'succeeded': False}
NAVIGATION_OR_CLEANUP = {
    'set_pos', 'SetPos', 'MoveDocBegin', 'MoveRight', 'MoveDocEnd', 'select_text', 'find',
    'Run', 'Undo', 'Close', 'Quit', 'Clear', 'Discard', 'save', 'save_as',
}
BUNDLE_STEP = {'op': 'official_numbering_apply', 'match': 'Item text', 'expected_page': 1, 'level': 2, 'confirm_layout': True}


class _Terminate(BaseException):
    """A representative BaseException that is neither KeyboardInterrupt nor SystemExit."""


ORDINARY = {
    'RuntimeError': lambda: RuntimeError('COM property failed'),
    'KeyError': lambda: KeyError('digest'),
}
CONTROL_FLOW = {
    'KeyboardInterrupt': lambda: KeyboardInterrupt(),
    'SystemExit': lambda: SystemExit(3),
    'BaseException': lambda: _Terminate('terminate'),
}

# (label, foreign definition, action whose Execute is faulted, fault points, executes attempted when the fault is raised)
PATHS = (
    ('existing-definition ParagraphShape', False, 'ParagraphShape',
     ('execute_before', 'execute_after', 'readback_shape', 'readback_outline', 'census', 'delta'), ['ParagraphShape']),
    ('definition-first OutlineNumber', True, 'OutlineNumber',
     ('execute_before', 'execute_after', 'readback_outline', 'readback_shape'), ['OutlineNumber']),
    ('definition-first ParagraphShape', True, 'ParagraphShape',
     ('execute_before', 'execute_after', 'readback_shape', 'readback_outline', 'census', 'delta'), ['OutlineNumber', 'ParagraphShape']),
)


class _FailingParameters:
    """HParameterSet that delegates, except that the named root attributes raise."""

    def __init__(self, original, failing, make_exc):
        object.__setattr__(self, '_original', original)
        object.__setattr__(self, '_failing', failing)
        object.__setattr__(self, '_make_exc', make_exc)

    def __getattr__(self, name):
        if name in self._failing:
            raise self._make_exc()
        return getattr(self._original, name)


class Fault:
    """Install one fault after a given mutating Execute; records what happened afterwards."""

    def __init__(self, hwp: FakeHwp, *, action: str, point: str, make_exc):
        self.hwp, self.action, self.point = hwp, action, point
        self.attempted: list[str] = []
        self.injected: list[BaseException] = []
        self.mark: int | None = None
        self._make_exc = make_exc
        original = hwp.HAction.Execute

        def run(name, hset):
            if name in {'OutlineNumber', 'ParagraphShape'}:
                self.attempted.append(name)
            if name == action:
                self.mark = len(hwp.calls)
                if point == 'execute_before':
                    raise self.raise_one()
            out = original(name, hset)
            if name == action:
                if point == 'execute_after':
                    raise self.raise_one()
                if point == 'readback_shape':
                    hwp.HParameterSet = _FailingParameters(hwp.HParameterSet, {'HParaShape'}, self.raise_one)
                if point == 'readback_outline':
                    hwp.HParameterSet = _FailingParameters(hwp.HParameterSet, {'HSecDef'}, self.raise_one)
            return out

        hwp.HAction.Execute = run

    def raise_one(self) -> BaseException:
        exc = self._make_exc()
        self.injected.append(exc)
        return exc

    def patches(self, stack: ExitStack) -> None:
        if self.point == 'census':
            real = nn._preflight_census

            def census(hwp):
                if self.mark is not None:
                    raise self.raise_one()
                return real(hwp)

            stack.enter_context(patch.object(nn, '_preflight_census', census))
        if self.point == 'delta':
            def delta(*_args, **_kwargs):
                raise self.raise_one()

            stack.enter_context(patch.object(nn, '_official_delta_problems', delta))

    def after_calls(self) -> list[str]:
        if self.mark is None:
            return []
        return [call[0] for call in self.hwp.calls[self.mark:] if call[0] in NAVIGATION_OR_CLEANUP]


def matrix():
    for path, foreign, action, points, attempted in PATHS:
        for point in points:
            for group, makers in (('ordinary', ORDINARY), ('control-flow', CONTROL_FLOW)):
                for name, make_exc in makers.items():
                    yield group, f'{path} :: {point} :: {name}', foreign, action, point, make_exc, attempted


def run_library(hwp):
    return nn.apply_official_numbering_at_match(hwp, find='Item text', level=2, protection=hwp.census)


class _Service(LocalCliBundleNumberingMixin):
    def _find_text_on_expected_page(self, *_args, **_kwargs):
        return {'page': 1}


def run_mixin(hwp):
    return _Service()._bundle_official_numbering_apply(hwp, dict(BUNDLE_STEP), protection=hwp.census)


def assert_tagged_noncomplete(case: unittest.TestCase, error: BaseException) -> None:
    case.assertIs(getattr(error, 'mutation_may_have_persisted', None), True)
    case.assertEqual(getattr(error, 'rollback', None), NO_CLEANUP)
    case.assertEqual(getattr(error, 'mutation_state', None), 'unknown')


class LibraryPropagationTests(unittest.TestCase):
    def run_case(self, foreign, action, point, make_exc):
        hwp = doc(foreign=foreign)
        hwp.cursor = (0, 2, 3)
        fault = Fault(hwp, action=action, point=point, make_exc=make_exc)
        return hwp, fault

    def test_ordinary_failure_after_a_reached_mutator_is_an_unknown_outcome(self) -> None:
        for group, label, foreign, action, point, make_exc, attempted in matrix():
            if group != 'ordinary':
                continue
            with self.subTest(label):
                hwp, fault = self.run_case(foreign, action, point, make_exc)
                with ExitStack() as stack:
                    fault.patches(stack)
                    with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                        run_library(hwp)
                assert_tagged_noncomplete(self, ctx.exception)
                self.assertEqual(len(fault.injected), 1, 'the failing step must not be retried')
                self.assertIs(ctx.exception.__cause__, fault.injected[0])
                self.assertEqual(fault.attempted, attempted)
                self.assertEqual(fault.after_calls(), [])

    def test_control_flow_exit_after_a_reached_mutator_propagates_tagged_and_unchanged(self) -> None:
        for group, label, foreign, action, point, make_exc, attempted in matrix():
            if group != 'control-flow':
                continue
            with self.subTest(label):
                hwp, fault = self.run_case(foreign, action, point, make_exc)
                with ExitStack() as stack:
                    fault.patches(stack)
                    with self.assertRaises(BaseException) as ctx:
                        run_library(hwp)
                self.assertEqual(len(fault.injected), 1)
                self.assertIs(ctx.exception, fault.injected[0], 'a termination signal must not be wrapped or swallowed')
                assert_tagged_noncomplete(self, ctx.exception)
                self.assertEqual(fault.attempted, attempted)
                self.assertEqual(fault.after_calls(), [])

    def test_uncertain_definition_stops_the_paragraph_write(self) -> None:
        for group, label, foreign, action, point, make_exc, attempted in matrix():
            if action != 'OutlineNumber':
                continue
            with self.subTest(label):
                hwp, fault = self.run_case(foreign, action, point, make_exc)
                with self.assertRaises(BaseException):
                    run_library(hwp)
                self.assertEqual(fault.attempted, ['OutlineNumber'])
                self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 0, 0])

    def test_success_control_binds_the_target_and_changes_nothing_else(self) -> None:
        for foreign, expected in ((False, ['ParagraphShape']), (True, ['OutlineNumber', 'ParagraphShape'])):
            with self.subTest(foreign=foreign):
                hwp = doc(foreign=foreign)
                fault = Fault(hwp, action='NoSuchAction', point='execute_after', make_exc=ORDINARY['RuntimeError'])
                result = run_library(hwp)
                self.assertEqual(result['matches'], 1)
                self.assertEqual(fault.attempted, expected)
                self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
                self.assertEqual([p['level'] for p in hwp.paragraphs], [0, 1, 0])

    def test_clean_typed_refusal_stays_a_refusal(self) -> None:
        hwp = doc(foreign=True, texts=('Other heading', 'Title', 'Item text'))
        hwp.paragraphs[0]['heading_type'] = 1
        fault = Fault(hwp, action='NoSuchAction', point='execute_after', make_exc=ORDINARY['RuntimeError'])
        with self.assertRaises(nn.NativeNumberingRefusal):
            run_library(hwp)
        self.assertEqual(fault.attempted, [])

    def test_ordinary_failure_before_any_mutator_is_not_relabelled(self) -> None:
        hwp = doc()
        fault = Fault(hwp, action='NoSuchAction', point='execute_after', make_exc=ORDINARY['RuntimeError'])
        hwp.HParameterSet = _FailingParameters(hwp.HParameterSet, {'HParaShape', 'HSecDef'}, ORDINARY['RuntimeError'])
        with self.assertRaises(RuntimeError) as ctx:
            run_library(hwp)
        self.assertNotIsInstance(ctx.exception, nn.EditOperationError)
        self.assertEqual(fault.attempted, [])


class BundleCaretTests(unittest.TestCase):
    def test_ordinary_failure_after_a_reached_mutator_is_a_mutation_error_without_navigation(self) -> None:
        for group, label, foreign, action, point, make_exc, attempted in matrix():
            if group != 'ordinary':
                continue
            with self.subTest(label):
                hwp = doc(foreign=foreign)
                hwp.cursor = (0, 2, 3)
                fault = Fault(hwp, action=action, point=point, make_exc=make_exc)
                with ExitStack() as stack:
                    fault.patches(stack)
                    with self.assertRaises(LocalCliMutationError) as ctx:
                        run_mixin(hwp)
                self.assertTrue(ctx.exception.mutation_may_have_persisted)
                self.assertEqual(ctx.exception.rollback, NO_CLEANUP)
                self.assertIs(ctx.exception.__cause__.__cause__, fault.injected[0])
                self.assertEqual(fault.attempted, attempted)
                self.assertEqual(fault.after_calls(), [], 'the caret must not move after an uncertain mutation')

    def test_control_flow_exit_after_a_reached_mutator_does_not_restore_the_caret(self) -> None:
        for group, label, foreign, action, point, make_exc, attempted in matrix():
            if group != 'control-flow':
                continue
            with self.subTest(label):
                hwp = doc(foreign=foreign)
                hwp.cursor = (0, 2, 3)
                fault = Fault(hwp, action=action, point=point, make_exc=make_exc)
                with ExitStack() as stack:
                    fault.patches(stack)
                    with self.assertRaises(BaseException) as ctx:
                        run_mixin(hwp)
                self.assertIs(ctx.exception, fault.injected[0])
                assert_tagged_noncomplete(self, ctx.exception)
                self.assertEqual(fault.attempted, attempted)
                self.assertEqual(fault.after_calls(), [], 'the caret must not move after an uncertain mutation')

    def test_success_restores_the_caret(self) -> None:
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                hwp = doc(foreign=foreign)
                hwp.cursor = (0, 2, 3)
                result = run_mixin(hwp)
                self.assertEqual(result['mutation'], 'official-numbering-apply')
                self.assertEqual(hwp.cursor, (0, 2, 3))
                self.assertEqual(hwp.calls[-1], ('set_pos', 0, 2, 3))

    def test_clean_typed_refusal_restores_the_caret(self) -> None:
        hwp = doc(foreign=True, texts=('Other heading', 'Title', 'Item text'))
        hwp.paragraphs[0]['heading_type'] = 1
        hwp.cursor = (0, 2, 3)
        with self.assertRaises(Exception) as ctx:
            run_mixin(hwp)
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(hwp.cursor, (0, 2, 3))
        self.assertEqual(hwp.calls[-1], ('set_pos', 0, 2, 3))

    def test_ordinary_failure_before_any_mutator_restores_the_caret_and_is_not_relabelled(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 2, 3)
        hwp.HParameterSet = _FailingParameters(hwp.HParameterSet, {'HParaShape', 'HSecDef'}, ORDINARY['RuntimeError'])
        with self.assertRaises(RuntimeError) as ctx:
            run_mixin(hwp)
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(hwp.cursor, (0, 2, 3))
        self.assertEqual(hwp.calls[-1], ('set_pos', 0, 2, 3))


class CommandBundlePropagationTests(unittest.TestCase):
    def test_ordinary_failure_after_a_reached_mutator_is_dirty_through_the_real_command_bundle(self) -> None:
        for group, label, foreign, action, point, make_exc, attempted in matrix():
            if group != 'ordinary':
                continue
            with self.subTest(label):
                hwp = _hwpml(foreign=foreign)
                hwp.cursor = (0, 2, 0)
                fault = Fault(hwp, action=action, point=point, make_exc=make_exc)
                with ExitStack() as stack:
                    fault.patches(stack)
                    result, persisted, _ = run_bundle(hwp, NUMBERING_STEP, real_snapshot=True)
                step = result['steps'][0]
                self.assertFalse(result['ok'])
                self.assertFalse(step['ok'])
                self.assertTrue(result['dirty'])
                self.assertTrue(persisted['dirty'])
                self.assertTrue(step['mutation_may_have_persisted'])
                self.assertEqual(step['rollback'], NO_CLEANUP)
                self.assertEqual(fault.attempted, attempted)
                self.assertEqual(fault.after_calls(), [])

    def test_control_flow_exit_propagates_out_of_the_real_command_bundle_tagged(self) -> None:
        for group, label, foreign, action, point, make_exc, attempted in matrix():
            if group != 'control-flow':
                continue
            with self.subTest(label):
                hwp = _hwpml(foreign=foreign)
                hwp.cursor = (0, 2, 0)
                fault = Fault(hwp, action=action, point=point, make_exc=make_exc)
                with ExitStack() as stack:
                    fault.patches(stack)
                    with self.assertRaises(BaseException) as ctx:
                        run_bundle(hwp, NUMBERING_STEP, real_snapshot=True)
                self.assertIs(ctx.exception, fault.injected[0])
                assert_tagged_noncomplete(self, ctx.exception)
                self.assertEqual(fault.attempted, attempted)
                self.assertEqual(fault.after_calls(), [])

    def test_success_and_clean_refusal_controls_are_not_dirty_when_nothing_was_attempted(self) -> None:
        hwp = _hwpml(foreign=True, texts=('Other heading', 'Title', 'Item text'))
        hwp.paragraphs[0]['heading_type'] = 1
        fault = Fault(hwp, action='NoSuchAction', point='execute_after', make_exc=ORDINARY['RuntimeError'])
        result, persisted, _ = run_bundle(hwp, NUMBERING_STEP, real_snapshot=True)
        step = result['steps'][0]
        self.assertFalse(result['ok'])
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertFalse(step['dirty'])
        self.assertFalse(persisted.get('dirty'))
        self.assertEqual(fault.attempted, [])

    def test_ordinary_failure_before_any_mutator_is_not_labelled_dirty(self) -> None:
        hwp = _hwpml()
        fault = Fault(hwp, action='NoSuchAction', point='execute_after', make_exc=ORDINARY['RuntimeError'])
        original = hwp.HParameterSet
        run_snapshot_ready = {'armed': False}
        original_find = hwp.find

        def find(*args, **kwargs):
            if not run_snapshot_ready['armed']:
                run_snapshot_ready['armed'] = True
                hwp.HParameterSet = _FailingParameters(original, {'HParaShape', 'HSecDef'}, ORDINARY['RuntimeError'])
            return original_find(*args, **kwargs)

        hwp.find = find
        result, persisted, _ = run_bundle(hwp, NUMBERING_STEP, real_snapshot=True)
        step = result['steps'][0]
        self.assertFalse(result['ok'])
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertFalse(step['dirty'])
        self.assertFalse(persisted.get('dirty'))
        self.assertEqual(fault.attempted, [])

    def test_success_then_a_later_failure_keeps_the_earlier_dirty_state(self) -> None:
        hwp = _hwpml()
        later = dict(NUMBERING_STEP, label='missing', match='not in the document')
        result, persisted, _ = run_bundle(hwp, NUMBERING_STEP, later, real_snapshot=True)
        self.assertFalse(result['ok'])
        self.assertTrue(result['steps'][0]['ok'])
        self.assertFalse(result['steps'][1]['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(persisted['dirty'])


if __name__ == '__main__':
    unittest.main()
