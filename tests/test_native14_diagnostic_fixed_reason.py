"""Offline tests: the official-numbering mutation errors carry a fixed, allowlisted failure judgement.

Every test drives the actual ``app.native_numbering`` over the declared fakes. Nothing here is native evidence and no test
claims a cause for the Native14 failure; they only prove which fixed judgement the product records at the point where it
adjudicates a failure, without any extra SDK call and without parsing any exception text.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app import native_numbering as nn
from tests.native_numbering_fakes import FakeCtrl, FakeHwp, _ParaShapeSet
from tests.test_native_numbering_review_contract import (
    PAGE_STEP,
    _Explosive,
    assert_unknown,
    doc,
    executes,
    hook_execute,
    hook_get_default,
    no_cleanup_calls,
    run_official,
)

SCHEMA = 'official-numbering-fixed-reason/1'
REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_AFTER_FAILURE = {'Run', 'Undo', 'Close', 'Discard', 'Quit', 'Clear', 'set_pos', 'MoveDocBegin', 'MoveRight', 'select_text', 'save', 'save_as'}
ARMED = [False]
HITS: list[tuple[str, str]] = []


class _HostileMeta(type):
    def __getattribute__(cls, name):
        if ARMED[0]:
            HITS.append(('metaclass', name))
            raise RuntimeError('hostile metaclass attribute')
        return super().__getattribute__(name)


class _HostileError(RuntimeError, metaclass=_HostileMeta):
    def __getattribute__(self, name):
        if ARMED[0]:
            HITS.append(('instance', name))
            raise RuntimeError('hostile instance attribute')
        return super().__getattribute__(name)


class _IntSub(int):
    pass


class _StrSub(str):
    pass


class _RaisingShape(_ParaShapeSet):
    """A ParagraphShape parameter set whose HeadingType write of OUTLINE raises (a declared test fake)."""

    @property
    def HeadingType(self):  # noqa: N802
        return self._heading

    @HeadingType.setter
    def HeadingType(self, value):  # noqa: N802
        if value == 1:
            raise RuntimeError('SECRET-SET-PAYLOAD')
        self._heading = value


def capture(case: unittest.TestCase, hwp: FakeHwp, **kwargs) -> nn.NativeNumberingMutationError:
    try:
        run_official(hwp, **kwargs)
    except nn.NativeNumberingMutationError as exc:
        case.addCleanup(ARMED.__setitem__, 0, False)
        ARMED[0] = False
        assert_unknown(case, exc)
        return exc
    finally:
        ARMED[0] = False
    raise AssertionError('no NativeNumberingMutationError was raised')


def assert_builtin_only(case: unittest.TestCase, value, depth=0) -> None:
    case.assertLess(depth, 4)
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            case.assertIs(type(key), str)
            assert_builtin_only(case, item, depth + 1)
    elif kind is list:
        for item in value:
            assert_builtin_only(case, item, depth + 1)
    else:
        case.assertIn(kind, (str, int, bool, type(None)))


def fixed(case: unittest.TestCase, error: nn.NativeNumberingMutationError) -> dict:
    reason = error.fixed_reason
    case.assertIs(type(reason), dict, reason)
    case.assertEqual(reason['schema'], SCHEMA)
    assert_builtin_only(case, reason)
    case.assertLess(len(json.dumps(reason)), 600)
    case.assertNotIn('SECRET', json.dumps(reason))
    return reason


def judgements(case: unittest.TestCase, error: nn.NativeNumberingMutationError) -> list[str]:
    return fixed(case, error)['judgements']


def nothing_after_last_execute(case: unittest.TestCase, hwp: FakeHwp) -> None:
    last = max(index for index, call in enumerate(hwp.calls) if call[0] == 'Execute')
    case.assertEqual([call for call in hwp.calls[last + 1:] if call[0] in FORBIDDEN_AFTER_FAILURE], [])
    no_cleanup_calls(case, hwp)


class OriginTests(unittest.TestCase):
    def test_product_module_is_this_repository(self):
        self.assertEqual(os.path.realpath(nn.__file__), str(REPO / 'app' / 'native_numbering.py'))


class PostconditionJudgementTests(unittest.TestCase):
    def test_paragraph_shape_that_changes_nothing_records_heading_and_level_together(self):
        hwp = doc()
        hwp.HAction.shape_mode = 'noop'
        error = capture(self, hwp, protection=None)
        reason = fixed(self, error)
        self.assertEqual(reason['judgements'], ['heading_type_mismatch', 'level_mismatch'])
        self.assertEqual(reason['values'], {'final_heading': 0, 'final_level': 0, 'native_level': 1})
        nothing_after_last_execute(self, hwp)

    def test_noop_with_the_census_also_records_the_census_delta_first(self):
        hwp = doc()
        hwp.HAction.shape_mode = 'noop'
        error = capture(self, hwp)
        self.assertEqual(judgements(self, error), ['census_delta_mismatch', 'heading_type_mismatch', 'level_mismatch'])

    def test_wrong_level_with_correct_heading(self):
        hwp = doc()
        hwp.HAction.shape_execute_level_offset = 1
        error = capture(self, hwp, protection=None)
        reason = fixed(self, error)
        self.assertEqual(reason['judgements'], ['level_mismatch'])
        self.assertEqual(reason['values'], {'final_level': 2, 'native_level': 1})
        self.assertEqual(judgements(self, capture(self, self._offset_doc())), ['census_delta_mismatch', 'level_mismatch'])

    @staticmethod
    def _offset_doc():
        hwp = doc()
        hwp.HAction.shape_execute_level_offset = 1
        return hwp

    def test_text_changed_by_the_action(self):
        hwp = doc()
        hwp.HAction.execute_text_edit = 'Changed text'
        self.assertEqual(judgements(self, capture(self, hwp, protection=None)), ['text_hash_mismatch'])
        hwp = doc()
        hwp.HAction.execute_text_edit = 'Changed text'
        self.assertEqual(judgements(self, capture(self, hwp)), ['census_delta_mismatch', 'text_hash_mismatch'])

    def test_main_list_control_sequence_changed(self):
        hwp = doc()
        hook_execute(hwp, after=lambda name: hwp.controls.append(FakeCtrl('gso')) if name == 'ParagraphShape' else None)
        error = capture(self, hwp)
        self.assertEqual(judgements(self, error), ['control_sequence_mismatch'])
        nothing_after_last_execute(self, hwp)

    def test_outline_definition_changed_by_the_action(self):
        def drift(name):
            if name == 'ParagraphShape':
                hwp.outline_shape = {**hwp.outline_shape, 'StrFormatLevel0': 'X^1'}
        hwp = doc()
        hook_execute(hwp, after=drift)
        self.assertEqual(judgements(self, capture(self, hwp, protection=None)), ['outline_definition_not_official'])
        hwp = doc()
        hook_execute(hwp, after=drift)
        self.assertEqual(judgements(self, capture(self, hwp)), ['outline_definition_not_official', 'census_delta_mismatch'])

    def test_unrelated_paragraph_changed_by_the_action(self):
        hwp = doc()
        hook_execute(hwp, after=lambda name: hwp.paragraphs[0].update(level=2) if name == 'ParagraphShape' else None)
        self.assertEqual(judgements(self, capture(self, hwp)), ['census_delta_mismatch'])

    def test_incomplete_after_census(self):
        hwp = doc()
        calls = []

        def reader():
            calls.append(1)
            census = hwp.census()
            if len(calls) > 1:
                census['frame'] = None
            return census
        error = capture(self, hwp, protection=reader)
        self.assertEqual(judgements(self, error), ['preservation_evidence_incomplete'])
        self.assertEqual(len(calls), 2)

    def test_after_census_read_failure_records_only_the_exception_class(self):
        hwp = doc()
        calls = []

        def reader():
            calls.append(1)
            if len(calls) > 1:
                raise OSError('SECRET-PATH-PAYLOAD')
            return hwp.census()
        reason = fixed(self, capture(self, hwp, protection=reader))
        self.assertEqual(reason['judgements'], ['protection_read_after_failed'])
        self.assertEqual(reason['exception_class'], 'OSError')
        self.assertNotIn('values', reason)

    def test_outcome_that_cannot_be_read_back(self):
        hwp = doc()
        hook_get_default(hwp, 'ParagraphShape', False, after_execute='ParagraphShape')
        error = capture(self, hwp)
        self.assertEqual(judgements(self, error), ['outcome_unverified'])
        nothing_after_last_execute(self, hwp)

    def test_unexpected_internal_exception_after_the_action(self):
        def boom(*_args, **_kwargs):
            raise ZeroDivisionError('SECRET-INTERNAL')
        hwp = doc()
        with patch.object(nn, '_official_delta_problems', boom):
            reason = fixed(self, capture(self, hwp))
        self.assertEqual(reason['judgements'], ['unexpected_exception_after_mutation'])
        self.assertEqual(reason['exception_class'], 'ZeroDivisionError')


class MidFlowJudgementTests(unittest.TestCase):
    def test_paragraph_shape_execute_raising(self):
        hwp = doc()
        hwp.HAction.execute_raises = RuntimeError('SECRET-EXECUTE')
        error = capture(self, hwp)
        reason = fixed(self, error)
        self.assertEqual((reason['judgements'], reason['exception_class']), (['paragraph_shape_execute_raised'], 'RuntimeError'))
        self.assertEqual(executes(hwp), ['ParagraphShape'])
        nothing_after_last_execute(self, hwp)

    def test_paragraph_shape_execute_not_confirmed(self):
        hwp = doc()
        hwp.HAction.execute_result = False
        reason = fixed(self, capture(self, hwp))
        self.assertEqual(reason['judgements'], ['paragraph_shape_not_confirmed'])
        self.assertEqual(reason['values'], {'observed': False})
        nothing_after_last_execute(self, hwp)

    def test_paragraph_shape_parameter_write_raising(self):
        hwp = doc()
        hwp.HParameterSet.HParaShape = _RaisingShape()
        reason = fixed(self, capture(self, hwp))
        self.assertEqual((reason['judgements'], reason['exception_class']), (['paragraph_shape_set_raised'], 'RuntimeError'))
        self.assertEqual(executes(hwp), [])

    def test_define_execute_raising(self):
        hwp = doc(foreign=True)
        hwp.HAction.execute_raises = ValueError('SECRET-DEFINE')
        reason = fixed(self, capture(self, hwp))
        self.assertEqual((reason['judgements'], reason['exception_class']), (['define_execute_raised'], 'ValueError'))
        self.assertEqual(executes(hwp), ['OutlineNumber'])
        nothing_after_last_execute(self, hwp)

    def test_define_not_confirmed(self):
        hwp = doc(foreign=True)
        hwp.HAction.execute_result = False
        reason = fixed(self, capture(self, hwp))
        self.assertEqual(reason['judgements'], ['define_not_confirmed'])
        self.assertEqual(reason['values'], {'observed': False})
        self.assertEqual(executes(hwp), ['OutlineNumber'])

    def test_define_that_reads_back_unofficial(self):
        hwp = doc(foreign=True)
        hwp.outline_mode = 'partial'
        reason = fixed(self, capture(self, hwp))
        self.assertEqual(reason['judgements'], ['define_readback_not_official'])
        self.assertEqual(executes(hwp), ['OutlineNumber'])
        nothing_after_last_execute(self, hwp)

    def test_define_that_cannot_be_read_back(self):
        hwp = doc(foreign=True)
        hook_get_default(hwp, 'OutlineNumber', False, after_execute='OutlineNumber')
        self.assertEqual(judgements(self, capture(self, hwp)), ['define_readback_unverified'])
        self.assertEqual(executes(hwp), ['OutlineNumber'])

    def test_paragraph_that_cannot_be_read_after_the_define(self):
        hwp = doc(foreign=True)
        hook_get_default(hwp, 'ParagraphShape', False, after_execute='OutlineNumber')
        self.assertEqual(judgements(self, capture(self, hwp)), ['paragraph_unverified_after_define'])
        self.assertEqual(executes(hwp), ['OutlineNumber'])

    def test_paragraph_state_changed_by_the_define(self):
        hwp = doc(foreign=True)
        hook_execute(hwp, after=lambda name: hwp.paragraphs[1].update(heading_type=1, level=2) if name == 'OutlineNumber' else None)
        reason = fixed(self, capture(self, hwp))
        self.assertEqual(reason['judgements'], ['paragraph_changed_during_define'])
        self.assertEqual(reason['values'], {'heading_before': 0, 'level_before': 0, 'heading_after': 1, 'level_after': 2})
        self.assertEqual(executes(hwp), ['OutlineNumber'])

    def test_caret_moved_by_the_define(self):
        hwp = doc(foreign=True)
        hook_execute(hwp, after=lambda name: setattr(hwp, 'cursor', (0, 2, 0)) if name == 'OutlineNumber' else None)
        error = capture(self, hwp)
        self.assertEqual(judgements(self, error), ['target_moved_after_define'])
        self.assertEqual(executes(hwp), ['OutlineNumber'])
        self.assertEqual([call for call in hwp.calls if call[0] == 'set_pos'][1:], [])


class PrimitiveBoundTests(unittest.TestCase):
    CASES = (
        (None, None), (0, 0), (1, 1), (-2147483648, -2147483648), (4294967295, 4294967295),
        (-2147483649, 'int-out-of-range'), (4294967296, 'int-out-of-range'), (2 ** 70, 'int-out-of-range'),
        (1.0, 'float'), (float('nan'), 'float'), ('True', 'str'), ('x' * 5000, 'str'),
        (_StrSub('SECRET'), 'noncanonical'), (_IntSub(1), 'noncanonical'), (object(), 'noncanonical'),
        ([True], 'noncanonical'), ({'ok': True}, 'noncanonical'), (_Explosive(), 'noncanonical'), (False, False),
    )

    def test_paragraph_shape_return_is_retained_as_a_bounded_primitive(self):
        for value, expected in self.CASES:
            with self.subTest(value=type(value).__name__, expected=expected):
                hwp = doc()
                hook_execute(hwp, returns={'ParagraphShape': value})
                reason = fixed(self, capture(self, hwp))
                self.assertEqual(reason['judgements'], ['paragraph_shape_not_confirmed'])
                self.assertIs(type(reason['values']['observed']), type(expected))
                self.assertEqual(reason['values']['observed'], expected)

    def test_define_return_is_retained_as_a_bounded_primitive(self):
        for value, expected in self.CASES:
            with self.subTest(value=type(value).__name__, expected=expected):
                hwp = doc(foreign=True)
                hook_execute(hwp, returns={'OutlineNumber': value})
                reason = fixed(self, capture(self, hwp))
                self.assertEqual(reason['judgements'], ['define_not_confirmed'])
                self.assertIs(type(reason['values']['observed']), type(expected))
                self.assertEqual(reason['values']['observed'], expected)


class HostileMetadataTests(unittest.TestCase):
    def setUp(self):
        HITS.clear()
        self.addCleanup(ARMED.__setitem__, 0, False)

    def test_hostile_exception_class_is_labelled_without_any_attribute_access(self):
        hwp = doc()

        def hostile(name):
            if name == 'ParagraphShape':
                ARMED[0] = True
                raise _HostileError('SECRET-HOSTILE')
        hook_execute(hwp, before=hostile)
        reason = fixed(self, capture(self, hwp))
        self.assertEqual(reason['judgements'], ['paragraph_shape_execute_raised'])
        self.assertEqual(reason['exception_class'], 'subclass-of:RuntimeError')
        self.assertEqual(HITS, [])

    def test_hostile_exception_in_the_post_action_wrapper(self):
        def boom(*_args, **_kwargs):
            ARMED[0] = True
            raise _HostileError('SECRET-HOSTILE')
        hwp = doc()
        with patch.object(nn, '_official_delta_problems', boom):
            reason = fixed(self, capture(self, hwp))
        self.assertEqual((reason['judgements'], reason['exception_class']), (['unexpected_exception_after_mutation'], 'subclass-of:RuntimeError'))
        self.assertEqual(HITS, [])

    def test_hostile_return_value_is_never_truth_tested_or_compared(self):
        hwp = doc()
        hook_execute(hwp, returns={'ParagraphShape': _Explosive()})
        reason = fixed(self, capture(self, hwp))
        self.assertEqual(reason['values'], {'observed': 'noncanonical'})

    def test_a_failing_reason_builder_never_changes_the_typed_error(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError('builder failed')
        hwp = doc()
        hwp.HAction.execute_result = False
        with patch.object(nn, '_fixed_primitive', broken):
            error = capture(self, hwp)
        self.assertIsNone(error.fixed_reason)
        self.assertIn('native ParagraphShape returned False', str(error))


class UnchangedBehaviourTests(unittest.TestCase):
    def test_messages_are_unchanged(self):
        hwp = doc()
        hwp.HAction.shape_mode = 'noop'
        self.assertEqual(
            str(capture(self, hwp, protection=None)),
            'outline numbering outcome is not as requested: HeadingType is 0; native level reads 0 instead of 1; nothing was rolled back',
        )
        hwp = doc()
        hwp.HAction.execute_result = False
        self.assertEqual(
            str(capture(self, hwp)),
            'native ParagraphShape returned False instead of the documented BOOL True; '
            'the paragraph may or may not have been changed and nothing was rolled back',
        )

    def test_legitimate_application_is_unchanged_and_carries_no_reason(self):
        hwp = doc()
        result = run_official(hwp)
        self.assertEqual((result['changed'], result['status'], result['matches']), (True, 'applied', 1))
        self.assertNotIn('fixed_reason', result)
        self.assertEqual(executes(hwp), ['ParagraphShape'])

    def test_refusals_are_still_refusals_without_a_reason(self):
        hwp = doc()
        hwp.paragraphs[1]['heading_type'] = 2
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp)
        self.assertFalse(hasattr(ctx.exception, 'fixed_reason') and ctx.exception.fixed_reason is not None)
        self.assertEqual(executes(hwp), [])

    def test_error_default_and_non_dict_reason_handling(self):
        self.assertIsNone(nn.NativeNumberingMutationError('x').fixed_reason)
        self.assertIsNone(nn.NativeNumberingMutationError('x', fixed_reason=['not', 'a', 'dict']).fixed_reason)
        self.assertEqual(nn.NativeNumberingMutationError('x', fixed_reason={'a': 1}).fixed_reason, {'a': 1})

    def test_page_number_failures_keep_their_page_schema(self):
        hwp = doc()
        hwp.HAction.page_number_mode = 'wrong-values'
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, nn.validate_page_number_step(PAGE_STEP), protection=None)
        reason = ctx.exception.fixed_reason
        self.assertIs(type(reason), dict)
        self.assertEqual(reason['schema'], 'page-number-fixed-reason/1')


if __name__ == '__main__':
    unittest.main()
