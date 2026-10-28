"""Review-finding regressions: ApplyClass/ApplyTo relation, target proof, native status domain, lifecycle, parity.

Synthetic fakes only. They prove call shapes, ordering and refusal/unknown-state handling of this
source; they are NOT Hancom evidence and make no native claim.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import edit_ops
from app import native_numbering as nn
from app.command_packages.runtime import get_command_package_registry
from app.local_cli_bundle_numbering import LocalCliBundleNumberingMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service import LocalCliService
from app.local_cli_service_support import LocalCliMutationError
from app.worker import _apply_edit_operations_with_optional_step_recorder
from tests.native_numbering_fakes import (
    OFFICIAL_SHAPE_LEVELS,
    STOCK_TAIL_LEVELS,
    FakeCtrl,
    FakeHwp,
    shape_items,
)
from tests.test_native_numbering_census import _HwpmlHwp

# ParameterSetTable_2504 (SecDef): ApplyTo 2/3/4 are only allowed when ApplyClass carries the matching flag.
DOCUMENTED_APPLY_CLASS_FLAG = {2: 0x04, 3: 0x08, 4: 0x10}
FOREIGN = (('Ⅰ.', 2), ('(^2)', 3), ('^3>', 4), ('[^4]', 1))
TAIL = {
    f'{prefix}Level{index}': value
    for index, (text, number_format) in enumerate(STOCK_TAIL_LEVELS, start=len(OFFICIAL_SHAPE_LEVELS))
    for prefix, value in (('StrFormat', text), ('NumFormat', number_format))
}
OFFICIAL = shape_items(OFFICIAL_SHAPE_LEVELS)
NO_CLEANUP = {'attempted': False, 'succeeded': False}
TEXTS = ('Title', 'Item text', 'Tail')


class _Explosive:
    """A native return value that must never be truth-tested or compared."""

    def __bool__(self) -> bool:
        raise AssertionError('result was truth-tested')

    def __eq__(self, other: object) -> bool:
        raise AssertionError('result was compared')

    __hash__ = None  # type: ignore[assignment]


def _unsupported_returns():
    return [None, 0, 1, 1.0, 'True', 'true', '', 'ok', object(), [], [True], (True,), {'ok': True}, _Explosive()]


def doc(*, foreign: bool = False, texts=TEXTS, cls=FakeHwp, **kwargs) -> FakeHwp:
    hwp = cls(paragraphs=[{'text': t, 'heading_type': 0, 'level': 0, 'section': 0} for t in texts], **kwargs)
    if foreign:
        hwp.outline_shape = {**shape_items(FOREIGN), **TAIL}
    return hwp


def executes(hwp: FakeHwp) -> list[str]:
    return [c[1] for c in hwp.calls if c[0] == 'Execute']


def headings(hwp: FakeHwp) -> list[int]:
    return [p['heading_type'] for p in hwp.paragraphs]


def run_official(hwp: FakeHwp, level: int = 2, find: str = 'Item text', protection='census'):
    reader = hwp.census if protection == 'census' else protection
    return nn.apply_official_numbering_at_match(hwp, find=find, level=level, protection=reader)


def _execute_shapes(hwp: FakeHwp) -> list[tuple[str, dict]]:
    rows = []
    for call in hwp.calls:
        if call[0] == 'Execute':
            params = {k: (dict(vars(v)) if hasattr(v, '__dict__') else v) for k, v in call[2].items()}
            rows.append((call[1], params))
    return rows


def hook_execute(hwp: FakeHwp, *, before=None, after=None, returns=None):
    original = hwp.HAction.Execute

    def run(name, hset):
        if before is not None:
            before(name)
        out = original(name, hset)
        if after is not None:
            after(name)
        if returns is not None and name in returns:
            return returns[name]
        return out

    hwp.HAction.Execute = run


def hook_get_default(hwp: FakeHwp, name: str, value, *, after_execute: str | None = None):
    original = hwp.HAction.GetDefault

    def run(called, hset):
        out = original(called, hset)
        if called == name and (after_execute is None or after_execute in executes(hwp)):
            return value
        return out

    hwp.HAction.GetDefault = run


def assert_unknown(case: unittest.TestCase, error: nn.NativeNumberingMutationError) -> None:
    case.assertEqual(error.mutation_state, 'unknown')
    case.assertTrue(error.mutation_may_have_persisted)
    case.assertEqual(error.rollback, NO_CLEANUP)


def no_cleanup_calls(case: unittest.TestCase, hwp: FakeHwp) -> None:
    case.assertEqual([c for c in hwp.calls if c[0] in {'Run', 'Undo', 'Close', 'Discard'}], [])


class ApplyClassRelationTests(unittest.TestCase):
    def test_module_constants_follow_the_documented_relation(self) -> None:
        apply_to = nn._OUTLINE_APPLY_TO_CURRENT_SECTION
        self.assertEqual(apply_to, 2)
        self.assertEqual(nn._OUTLINE_APPLY_CLASS, DOCUMENTED_APPLY_CLASS_FLAG[apply_to])

    def test_every_level_sends_the_smallest_class_that_permits_current_section(self) -> None:
        for level in (1, 2, 3, 4):
            hwp = doc(foreign=True)
            other_before = dict(hwp.other_section_shape)
            run_official(hwp, level)
            sent = [c[2] for c in hwp.calls if c[0] == 'Execute' and c[1] == 'OutlineNumber']
            self.assertEqual(len(sent), 1, msg=level)
            params = sent[0]
            self.assertEqual(params['ApplyTo'], 2, msg=level)
            self.assertIs(type(params['ApplyClass']), int, msg=level)
            self.assertTrue(params['ApplyClass'] & DOCUMENTED_APPLY_CLASS_FLAG[params['ApplyTo']], msg=level)
            self.assertEqual(params['ApplyClass'], DOCUMENTED_APPLY_CLASS_FLAG[params['ApplyTo']], msg=level)
            self.assertEqual(hwp.other_section_shape, other_before, msg=level)

    def test_foreign_outline_in_the_same_section_is_still_refused_with_the_corrected_class(self) -> None:
        hwp = doc(foreign=True, texts=('Other heading', *TEXTS))
        hwp.paragraphs[0]['heading_type'] = 1
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, 1)
        self.assertEqual(ctx.exception.code, 'foreign_outline_definition')
        self.assertEqual(executes(hwp), [])

    def test_widened_scope_is_still_an_unknown_outcome(self) -> None:
        hwp = doc(foreign=True)
        hwp.other_section_shape = {**shape_items(FOREIGN[::-1]), **TAIL}
        hwp.paragraphs.append({'text': 'Next section', 'heading_type': 0, 'level': 0, 'section': 1})
        original = hwp.HAction.Execute

        def widen(name, hset):
            out = original(name, hset)
            if name == 'OutlineNumber':
                hwp.other_section_shape.update(vars(hset.OutlineShape))
            return out

        hwp.HAction.Execute = widen
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            run_official(hwp, 2)
        assert_unknown(self, ctx.exception)


class TargetDriftTests(unittest.TestCase):
    DESTINATIONS = {'title': (0, 0, 0), 'tail': (0, 2, 0), 'same-paragraph-offset': (0, 1, 3), 'other-list': (1, 1, 0)}

    def _protection_that_moves(self, hwp, dest):
        def reader():
            value = hwp.census()
            hwp.cursor = dest
            return value

        return reader

    def test_protection_reader_moving_the_caret_refuses_before_any_execute(self) -> None:
        for foreign in (False, True):
            for label, dest in self.DESTINATIONS.items():
                with self.subTest(foreign=foreign, destination=label):
                    hwp = doc(foreign=foreign)
                    before = [dict(p) for p in hwp.paragraphs]
                    shape_before = dict(hwp.outline_shape)
                    with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                        run_official(hwp, 2, protection=self._protection_that_moves(hwp, dest))
                    self.assertEqual(ctx.exception.mutation_state, 'none')
                    self.assertEqual(executes(hwp), [])
                    self.assertEqual(hwp.paragraphs, before)
                    self.assertEqual(hwp.outline_shape, shape_before)
                    self.assertEqual(hwp.cursor, dest, 'a drifted caret must not be recaptured or restored as the target')

    def test_getter_moving_the_caret_refuses_before_any_execute(self) -> None:
        for foreign, getter in ((False, 'ParagraphShape'), (True, 'ParagraphShape'), (True, 'OutlineNumber'), (False, 'OutlineNumber')):
            with self.subTest(foreign=foreign, getter=getter):
                hwp = doc(foreign=foreign)
                original = hwp.HAction.GetDefault

                def move(name, hset, original=original, getter=getter, hwp=hwp):
                    out = original(name, hset)
                    if name == getter:
                        hwp.cursor = (0, 0, 0)
                    return out

                hwp.HAction.GetDefault = move
                with self.assertRaises(nn.NativeNumberingRefusal):
                    run_official(hwp, 2)
                self.assertEqual(executes(hwp), [])
                self.assertEqual(headings(hwp), [0, 0, 0])

    def test_caret_lost_after_a_valid_definition_stops_before_the_paragraph_write(self) -> None:
        for label, dest in self.DESTINATIONS.items():
            with self.subTest(destination=label):
                hwp = doc(foreign=True)
                hook_execute(hwp, after=lambda name, hwp=hwp, dest=dest: setattr(hwp, 'cursor', dest) if name == 'OutlineNumber' else None)
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    run_official(hwp, 2)
                assert_unknown(self, ctx.exception)
                self.assertEqual(executes(hwp), ['OutlineNumber'])
                self.assertEqual(headings(hwp), [0, 0, 0])
                self.assertTrue(all(hwp.outline_shape[k] == v for k, v in OFFICIAL.items()))
                self.assertEqual(hwp.cursor, dest, 'no automatic caret recapture')
                no_cleanup_calls(self, hwp)

    def test_selection_change_after_a_valid_definition_stops_before_the_paragraph_write(self) -> None:
        hwp = doc(foreign=True)
        hook_execute(hwp, after=lambda name: setattr(hwp, 'selection_para', 0) if name == 'OutlineNumber' else None)
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            run_official(hwp, 2)
        assert_unknown(self, ctx.exception)
        self.assertEqual(executes(hwp), ['OutlineNumber'])
        self.assertEqual(headings(hwp), [0, 0, 0])

    def test_native_redirect_inside_the_last_write_is_detected_after_the_call_not_claimed_atomic(self) -> None:
        hwp = doc()
        hook_execute(hwp, before=lambda name: setattr(hwp, 'cursor', (0, 0, 0)) if name == 'ParagraphShape' else None)
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            run_official(hwp, 2)
        assert_unknown(self, ctx.exception)
        self.assertEqual(executes(hwp), ['ParagraphShape'])
        no_cleanup_calls(self, hwp)

    def test_selection_outside_the_target_refuses_before_any_execute(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 1, 0)
        hwp.selection_para = 0
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_official_numbering(hwp, {'level': 2}, protection=hwp.census, target_find='Item text')
        self.assertEqual(executes(hwp), [])

    def test_selection_spanning_paragraphs_refuses_before_any_execute(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 1, 0)
        hwp.get_selected_pos = lambda: (True, 0, 1, 0, 0, 2, 4)
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_official_numbering(hwp, {'level': 2}, protection=hwp.census, target_find='Item text')
        self.assertEqual(executes(hwp), [])

    def test_selection_growing_during_the_census_refuses_before_any_execute(self) -> None:
        hwp = doc()

        def grow():
            value = hwp.census()
            hwp.selection_para = 0
            return value

        with self.assertRaises(nn.NativeNumberingRefusal):
            run_official(hwp, 2, protection=grow)
        self.assertEqual(executes(hwp), [])

    def test_unchanged_selection_inside_the_target_paragraph_is_accepted(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 1, 0)
        hwp.selection_para = 1
        result = nn.apply_official_numbering(hwp, {'level': 2}, protection=hwp.census, target_find='Item text')
        self.assertTrue(result['changed'])
        self.assertEqual(headings(hwp), [0, 1, 0])
        self.assertEqual(executes(hwp), ['ParagraphShape'])

    def test_direct_call_anchors_the_entry_caret(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 1, 0)

        def reader():
            value = hwp.census()
            hwp.cursor = (0, 2, 0)
            return value

        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_official_numbering(hwp, {'level': 2}, protection=reader, target_find='Item text')
        self.assertEqual(executes(hwp), [])


class PositionTypeTests(unittest.TestCase):
    BAD_POSITIONS = {
        'float': lambda p: tuple(float(x) for x in p),
        'str': lambda p: tuple(str(x) for x in p),
        'bool': lambda p: tuple(bool(x) for x in p),
        'negative': lambda p: (p[0], -1, p[2]),
        'short': lambda p: p[:2],
        'long': lambda p: (*p, 0),
        'none': lambda p: None,
        'int-subclass': lambda p: tuple(type('MyInt', (int,), {})(x) for x in p),
    }

    def test_noncanonical_get_pos_cannot_prove_a_target(self) -> None:
        for label, convert in self.BAD_POSITIONS.items():
            with self.subTest(converter=label):
                hwp = doc(foreign=True)
                hwp.get_pos = lambda hwp=hwp, convert=convert: convert(hwp.cursor)
                with self.assertRaises((nn.NativeNumberingRefusal, nn.NativeNumberingMutationError)):
                    run_official(hwp, 2)
                self.assertEqual(executes(hwp), [])

    def test_noncanonical_get_pos_after_a_valid_definition_stops_before_the_paragraph_write(self) -> None:
        for label, convert in self.BAD_POSITIONS.items():
            with self.subTest(converter=label):
                hwp = doc(foreign=True)

                def after(name, hwp=hwp, convert=convert):
                    if name == 'OutlineNumber':
                        hwp.get_pos = lambda: convert(hwp.cursor)

                hook_execute(hwp, after=after)
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    run_official(hwp, 2)
                assert_unknown(self, ctx.exception)
                self.assertEqual(executes(hwp), ['OutlineNumber'])

    def test_failing_get_pos_after_a_valid_definition_stops_before_the_paragraph_write(self) -> None:
        hwp = doc(foreign=True)

        def broken():
            raise RuntimeError('GetPos failed')

        hook_execute(hwp, after=lambda name: setattr(hwp, 'get_pos', broken) if name == 'OutlineNumber' else None)
        with self.assertRaises(nn.NativeNumberingMutationError):
            run_official(hwp, 2)
        self.assertEqual(executes(hwp), ['OutlineNumber'])

    def test_noncanonical_selected_position_cannot_locate_a_target(self) -> None:
        bad = {
            'int-flag': (1, 0, 1, 0, 0, 1, 9),
            'str-flag': ('True', 0, 1, 0, 0, 1, 9),
            'float-coordinate': (True, 0.0, 1.0, 0.0, 0.0, 1.0, 9.0),
            'str-coordinate': (True, '0', '1', '0', '0', '1', '9'),
            'bool-coordinate': (True, False, True, False, False, True, 9),
            'negative': (True, 0, -1, 0, 0, -1, 9),
            'short': (True, 0, 1, 0, 0, 1),
            'none': None,
        }
        for label, value in bad.items():
            with self.subTest(selected=label):
                hwp = doc(foreign=True)
                hwp.get_selected_pos = lambda value=value: value
                with self.assertRaises((nn.NativeNumberingRefusal, nn.NativeNumberingMutationError)):
                    run_official(hwp, 2)
                self.assertEqual(executes(hwp), [])

    def test_noncanonical_selected_position_at_the_boundary_stops_the_write(self) -> None:
        hwp = doc(foreign=True)

        def after(name):
            if name == 'OutlineNumber':
                hwp.get_selected_pos = lambda: (True, 0.0, 1.0, 0.0, 0.0, 1.0, 9.0)

        hook_execute(hwp, after=after)
        with self.assertRaises(nn.NativeNumberingMutationError):
            run_official(hwp, 2)
        self.assertEqual(executes(hwp), ['OutlineNumber'])

    def test_list_and_tuple_canonical_positions_are_accepted(self) -> None:
        for wrap in (list, tuple):
            hwp = doc()
            hwp.get_pos = lambda hwp=hwp, wrap=wrap: wrap(hwp.cursor)
            run_official(hwp, 2)
            self.assertEqual(headings(hwp), [0, 1, 0])

    def test_caret_restore_arguments_are_exact_integers(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 2, 5)
        nn.apply_page_number(hwp, {'position': 'bottom-center'}, protection=hwp.census)
        restores = [c for c in hwp.calls if c[0] == 'set_pos']
        self.assertTrue(restores)
        self.assertEqual(restores[-1][1:], (0, 2, 5))
        self.assertTrue(all(type(v) is int for call in restores for v in call[1:]))
        self.assertEqual(hwp.cursor, (0, 2, 5))

    def test_noncanonical_caret_before_a_page_number_is_never_coerced_into_a_restore(self) -> None:
        hwp = doc()
        hwp.get_pos = lambda: (0.0, 2.0, 5.0)
        result = nn.apply_page_number(hwp, {'position': 'bottom-center'}, protection=hwp.census)
        self.assertEqual([c for c in hwp.calls if c[0] == 'set_pos'], [])
        self.assertTrue(any('could not be read' in w for w in result['warnings']))


class NativeStatusDomainTests(unittest.TestCase):
    def test_unsupported_execute_returns_after_a_real_effect_are_unknown_for_the_page_number(self) -> None:
        for value in _unsupported_returns() + [False]:
            with self.subTest(value=type(value).__name__ + repr(value if not isinstance(value, _Explosive) else '')):
                hwp = doc()
                hook_execute(hwp, returns={'PageNumPos': value})
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    nn.apply_page_number(hwp, {'position': 'bottom-center'}, protection=hwp.census)
                assert_unknown(self, ctx.exception)
                self.assertEqual(executes(hwp), ['PageNumPos'])
                self.assertEqual([c.CtrlID for c in hwp.controls].count('pgnp'), 1, 'the effect is kept, never rolled back')
                no_cleanup_calls(self, hwp)

    def test_unsupported_paragraph_shape_return_after_a_real_effect_is_unknown(self) -> None:
        for value in _unsupported_returns() + [False]:
            with self.subTest(value=type(value).__name__):
                hwp = doc()
                hook_execute(hwp, returns={'ParagraphShape': value})
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    run_official(hwp, 2)
                assert_unknown(self, ctx.exception)
                self.assertEqual(executes(hwp), ['ParagraphShape'])
                self.assertEqual(headings(hwp), [0, 1, 0], 'the effect is kept, never rolled back')
                no_cleanup_calls(self, hwp)

    def test_unsupported_outline_return_after_a_real_effect_is_unknown_and_stops_further_edits(self) -> None:
        for value in _unsupported_returns() + [False]:
            with self.subTest(value=type(value).__name__):
                hwp = doc(foreign=True)
                hook_execute(hwp, returns={'OutlineNumber': value})
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    run_official(hwp, 2)
                assert_unknown(self, ctx.exception)
                self.assertEqual(executes(hwp), ['OutlineNumber'])
                self.assertEqual(headings(hwp), [0, 0, 0])
                self.assertTrue(all(hwp.outline_shape[k] == v for k, v in OFFICIAL.items()))

    def test_unsupported_get_default_before_any_mutation_is_a_clean_refusal(self) -> None:
        cases = (
            ('page', 'PageNumPos', False),
            ('official-outline', 'OutlineNumber', False),
            ('official-shape', 'ParagraphShape', False),
            ('official-shape-define', 'ParagraphShape', True),
        )
        for value in _unsupported_returns() + [False]:
            for label, getter, foreign in cases:
                with self.subTest(value=type(value).__name__, case=label):
                    hwp = doc(foreign=foreign)
                    hook_get_default(hwp, getter, value)
                    with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                        if label == 'page':
                            nn.apply_page_number(hwp, {'position': 'bottom-center'}, protection=hwp.census)
                        else:
                            run_official(hwp, 2)
                    self.assertEqual(ctx.exception.mutation_state, 'none')
                    self.assertEqual(executes(hwp), [])

    def test_get_default_failure_after_a_definition_is_never_a_clean_refusal(self) -> None:
        for value in _unsupported_returns() + [False]:
            for getter in ('OutlineNumber', 'ParagraphShape'):
                with self.subTest(value=type(value).__name__, getter=getter):
                    hwp = doc(foreign=True)
                    hook_get_default(hwp, getter, value, after_execute='OutlineNumber')
                    with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                        run_official(hwp, 2)
                    assert_unknown(self, ctx.exception)
                    self.assertEqual(executes(hwp), ['OutlineNumber'])
                    self.assertTrue(all(hwp.outline_shape[k] == v for k, v in OFFICIAL.items()))

    def test_get_default_failure_after_the_paragraph_write_is_unknown(self) -> None:
        for value in _unsupported_returns() + [False]:
            for getter in ('OutlineNumber', 'ParagraphShape'):
                with self.subTest(value=type(value).__name__, getter=getter):
                    hwp = doc()
                    hook_get_default(hwp, getter, value, after_execute='ParagraphShape')
                    with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                        run_official(hwp, 2)
                    assert_unknown(self, ctx.exception)
                    self.assertEqual(headings(hwp), [0, 1, 0])

    def test_true_is_the_supported_success_for_both_actions(self) -> None:
        hwp = doc()
        hook_execute(hwp, returns={'PageNumPos': True})
        hook_get_default(hwp, 'PageNumPos', True)
        result = nn.apply_page_number(hwp, {'position': 'bottom-center'}, protection=hwp.census)
        self.assertEqual(result['status'], 'inserted')
        for foreign, expected in ((False, ['ParagraphShape']), (True, ['OutlineNumber', 'ParagraphShape'])):
            hwp = doc(foreign=foreign)
            hook_execute(hwp, returns={'OutlineNumber': True, 'ParagraphShape': True})
            result = run_official(hwp, 3)
            self.assertTrue(result['changed'])
            self.assertEqual(executes(hwp), expected)
            self.assertEqual(hwp.paragraphs[1]['level'], 2)

    def test_native_unknown_does_not_prevent_the_idempotent_second_request(self) -> None:
        hwp = doc()
        run_official(hwp, 2)
        calls_before = len(hwp.calls)
        result = run_official(hwp, 2)
        self.assertFalse(result['changed'])
        self.assertEqual(result['status'], 'already_applied')
        self.assertEqual([c for c in hwp.calls[calls_before:] if c[0] == 'Execute'], [])


class PositivePathTests(unittest.TestCase):
    def test_existing_and_scoped_definitions_all_levels(self) -> None:
        for foreign, expected in ((False, ['ParagraphShape']), (True, ['OutlineNumber', 'ParagraphShape'])):
            for level in (1, 2, 3, 4):
                with self.subTest(foreign=foreign, level=level):
                    hwp = doc(foreign=foreign)
                    result = run_official(hwp, level)
                    self.assertEqual(executes(hwp), expected)
                    self.assertTrue(result['changed'])
                    self.assertEqual(headings(hwp), [0, 1, 0])
                    self.assertEqual(hwp.paragraphs[1]['level'], level - 1)
                    self.assertEqual([p['text'] for p in hwp.paragraphs], list(TEXTS))
                    self.assertIs(result['numbering_format_enforced'], True)
                    self.assertTrue(result['protection']['checked'])


def _hwpml(foreign: bool = False, texts=TEXTS) -> _HwpmlHwp:
    return doc(foreign=foreign, texts=texts, cls=_HwpmlHwp, controls=[FakeCtrl('secd')])


PAGE_STEP = {'op': 'page_number_apply', 'label': 'page', 'position': 'bottom-center', 'start_number': 1, 'side_char': 'none', 'confirm_layout': True}
NUMBERING_STEP = {'op': 'official_numbering_apply', 'label': 'numbering', 'match': 'Item text', 'expected_page': 1, 'level': 2, 'confirm_layout': True}


def run_bundle(hwp, *steps, real_snapshot: bool = False):
    service = object.__new__(LocalCliService)
    binding = {'session_id': 'session-numbering', 'command_generation': 0, 'native_command_sequence': 0}
    persisted: dict[str, object] = {}
    snapshots: list[int] = []
    service.command_packages = get_command_package_registry()
    service._load_active_binding = lambda session_id=None: dict(binding)  # type: ignore[method-assign]

    def snapshot(handle_hwp):
        snapshots.append(1)
        return {'snapshot': True}

    if not real_snapshot:
        service._bundle_compact_snapshot = snapshot  # type: ignore[method-assign]
    # Caller-owned fixture models the runtime handle's sticky producer intent;
    # no custody callback is supplied, so signal propagation is still tested.
    handle = SimpleNamespace(hwp=hwp, source_filename='document.hwpx', session_id=binding['session_id'], mutation_intent=False, hold_native_custody=None)
    handle.mark_mutating_intent = lambda: setattr(handle, 'mutation_intent', True)
    service._execute_live = lambda *, handler, **kwargs: handler(handle)  # type: ignore[method-assign]

    def find(handle_hwp, *, match, expected_page):
        for index, para in enumerate(handle_hwp.paragraphs):
            if match in str(para['text']):
                handle_hwp.cursor = (0, index, 0)
                return {'page': expected_page, 'pos': [0, index, 0]}
        raise LocalCliRuntimeError('target not found')

    def update_live_binding(current, **kwargs):
        persisted.update(kwargs)
        return current

    service._find_text_on_expected_page = find  # type: ignore[method-assign]
    service._update_live_binding = update_live_binding  # type: ignore[method-assign]
    service._save_binding = lambda current: current  # type: ignore[method-assign]
    service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]
    service._validated_artifact_projection = lambda **kwargs: {}  # type: ignore[method-assign]
    service._public_bundle_steps = lambda **kwargs: kwargs['steps']  # type: ignore[method-assign]
    service._public_artifacts = lambda **kwargs: {}  # type: ignore[method-assign]
    service._binding_session_id = lambda current: current['session_id']  # type: ignore[method-assign]
    with patch('app.local_cli_service.snapshot_live_location', return_value={}):
        result = service.command_bundle(steps=[dict(step) for step in steps])
    return result, persisted, snapshots


class LifecyclePropagationTests(unittest.TestCase):
    def test_none_after_an_effect_is_an_unknown_outcome_through_the_real_bundle(self) -> None:
        for label, step, action in (('page', PAGE_STEP, 'PageNumPos'), ('official', NUMBERING_STEP, 'ParagraphShape')):
            with self.subTest(op=label):
                hwp = _hwpml()
                hook_execute(hwp, returns={action: None})
                result, persisted, _snapshots = run_bundle(hwp, step)
                self.assertFalse(result['ok'])
                self.assertTrue(result['dirty'])
                self.assertTrue(persisted['dirty'])
                first = result['steps'][0]
                self.assertFalse(first['ok'])
                self.assertTrue(first['mutation_may_have_persisted'])
                self.assertEqual(first['rollback'], NO_CLEANUP)
                self.assertEqual(first['mutation'], {'may_have_persisted': True, 'rollback': NO_CLEANUP})

    def test_unknown_outcome_prevents_the_following_numbering_edit(self) -> None:
        for first, second, action in ((PAGE_STEP, NUMBERING_STEP, 'PageNumPos'), (NUMBERING_STEP, PAGE_STEP, 'ParagraphShape')):
            with self.subTest(first=first['op']):
                hwp = _hwpml()
                hook_execute(hwp, returns={action: None})
                result, _persisted, _snapshots = run_bundle(hwp, first, second)
                self.assertFalse(result['ok'])
                self.assertEqual(len(executes(hwp)), 1, 'no further native edit after an unknown outcome')

    def test_drift_between_definition_and_write_is_an_unknown_outcome_through_the_real_bundle(self) -> None:
        hwp = _hwpml(foreign=True)
        hook_execute(hwp, after=lambda name: setattr(hwp, 'cursor', (0, 0, 0)) if name == 'OutlineNumber' else None)
        result, persisted, _snapshots = run_bundle(hwp, NUMBERING_STEP)
        self.assertFalse(result['ok'])
        first = result['steps'][0]
        self.assertTrue(first['mutation_may_have_persisted'])
        self.assertTrue(persisted['dirty'])
        self.assertEqual(executes(hwp), ['OutlineNumber'])
        self.assertEqual(headings(hwp), [0, 0, 0])

    def test_pre_mutation_drift_is_a_clean_refusal_through_the_real_bundle(self) -> None:
        hwp = _hwpml(foreign=True)
        original = hwp.census

        def moving():
            value = original()
            hwp.cursor = (0, 0, 0)
            return value

        with patch('app.native_numbering_census.live_protection_reader', return_value=moving):
            result, persisted, _snapshots = run_bundle(hwp, NUMBERING_STEP)
        self.assertFalse(result['ok'])
        first = result['steps'][0]
        self.assertFalse(first['mutation_may_have_persisted'])
        self.assertFalse(first['dirty'])
        self.assertEqual(executes(hwp), [])

    def test_api_and_worker_seams_keep_the_unknown_outcome(self) -> None:
        operations = (
            ({'op': 'page_number', 'position': 'bottom-center'}, 'PageNumPos'),
            ({'op': 'official_numbering', 'find': 'Item text', 'level': 2}, 'ParagraphShape'),
        )
        for apply in (edit_ops.apply_edit_operations, _apply_edit_operations_with_optional_step_recorder):
            for operation, action in operations:
                with self.subTest(apply=apply.__name__, op=operation['op']):
                    hwp = _hwpml()
                    hook_execute(hwp, returns={action: None})
                    with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                        apply(hwp, edit_ops.normalize_operations([operation]))
                    assert_unknown(self, ctx.exception)
                    self.assertEqual(len(executes(hwp)), 1)

    def test_api_pre_mutation_drift_is_a_clean_refusal(self) -> None:
        hwp = _hwpml(foreign=True)
        original = hwp.census

        def moving():
            value = original()
            hwp.cursor = (0, 2, 0)
            return value

        operation = edit_ops.normalize_operations([{'op': 'official_numbering', 'find': 'Item text', 'level': 2}])
        with patch('app.native_numbering_census.live_protection_reader', return_value=moving):
            with self.assertRaises(nn.NativeNumberingRefusal):
                edit_ops.apply_edit_operations(hwp, operation)
        self.assertEqual(executes(hwp), [])


class SurfaceParityTests(unittest.TestCase):
    """The protected CLI/MCP step and the job-API op drive one engine; this pins the field mapping and refusals."""

    def _api(self, hwp, level, find='Item text'):
        return edit_ops.apply_edit_operations(hwp, edit_ops.normalize_operations([{'op': 'official_numbering', 'find': find, 'level': level}]))[0]

    def _bundle(self, hwp, level, match='Item text'):
        return run_bundle(hwp, {**NUMBERING_STEP, 'match': match, 'level': level})[0]

    def test_same_request_gives_the_same_native_effect_and_format_result(self) -> None:
        for foreign in (False, True):
            for level in (1, 2, 3, 4):
                with self.subTest(foreign=foreign, level=level):
                    api_hwp, cli_hwp = _hwpml(foreign), _hwpml(foreign)
                    api = self._api(api_hwp, level)
                    cli = self._bundle(cli_hwp, level)
                    self.assertTrue(cli['ok'])
                    cli_result = cli['steps'][0]['result']['result']
                    self.assertEqual(api_hwp.paragraphs, cli_hwp.paragraphs)
                    self.assertEqual(api_hwp.outline_shape, cli_hwp.outline_shape)
                    self.assertEqual(_execute_shapes(api_hwp), _execute_shapes(cli_hwp))
                    for key in ('changed', 'status', 'level', 'native_level', 'numbering_format', 'native_actions', 'protection'):
                        self.assertEqual(api[key], cli_result[key], msg=key)

    def test_same_refusals_with_zero_native_mutation(self) -> None:
        cases = (
            ('ambiguous', dict(texts=('Item one', 'Item two', 'Tail')), 'Item'),
            ('absent', {}, 'Nowhere'),
        )
        for label, kwargs, find in cases:
            with self.subTest(case=label):
                api_hwp, cli_hwp = _hwpml(**kwargs), _hwpml(**kwargs)
                with self.assertRaises(nn.NativeNumberingRefusal):
                    self._api(api_hwp, 2, find)
                cli = self._bundle(cli_hwp, 2, find)
                self.assertFalse(cli['ok'])
                self.assertFalse(cli['steps'][0]['mutation_may_have_persisted'])
                self.assertEqual(executes(api_hwp), [])
                self.assertEqual(executes(cli_hwp), [])
        for label, heading in (('bullet', 3), ('number', 2)):
            with self.subTest(case=label):
                api_hwp, cli_hwp = _hwpml(), _hwpml()
                for hwp in (api_hwp, cli_hwp):
                    hwp.paragraphs[1]['heading_type'] = heading
                with self.assertRaises(nn.NativeNumberingRefusal):
                    self._api(api_hwp, 2)
                self.assertFalse(self._bundle(cli_hwp, 2)['ok'])
                self.assertEqual(executes(api_hwp) + executes(cli_hwp), [])

    def test_invalid_levels_are_refused_identically_with_zero_native_calls(self) -> None:
        for level in (0, 5, True, 2.0, '2', None):
            with self.subTest(level=level):
                with self.assertRaises(edit_ops.EditOperationError):
                    edit_ops.normalize_operations([{'op': 'official_numbering', 'find': 'Item text', 'level': level}])
                hwp = _hwpml()
                with self.assertRaises(Exception):
                    run_bundle(hwp, {**NUMBERING_STEP, 'level': level})
                self.assertEqual(hwp.calls, [])

    def test_api_never_silently_drops_the_protected_page_and_layout_proof_fields(self) -> None:
        for extra in ({'expected_page': 1}, {'confirm_layout': True}, {'match': 'Item text'}):
            with self.subTest(extra=sorted(extra)):
                with self.assertRaises(edit_ops.EditOperationError):
                    edit_ops.normalize_operations([{'op': 'official_numbering', 'find': 'Item text', 'level': 2, **extra}])

    def test_protected_step_keeps_confirm_layout_and_expected_page(self) -> None:
        for override in ({'confirm_layout': False}, {'expected_page': 0}, {'expected_page': None}):
            with self.subTest(override=override):
                hwp = _hwpml()
                step = {k: v for k, v in {**NUMBERING_STEP, **override}.items() if v is not None}
                with self.assertRaises(Exception):
                    run_bundle(hwp, step)
                self.assertEqual(hwp.calls, [])


class PageNumberParityAndSnapshotTests(unittest.TestCase):
    API_PAGE = {'op': 'page_number', 'position': 'bottom-center', 'start_number': 3, 'prefix': '-', 'suffix': '-'}
    STEP_PAGE = {**PAGE_STEP, 'start_number': 3, 'side_char': 'dash'}

    def test_page_number_api_op_and_protected_step_drive_the_same_native_call(self) -> None:
        api_hwp, cli_hwp = _hwpml(), _hwpml()
        api = edit_ops.apply_edit_operations(api_hwp, edit_ops.normalize_operations([dict(self.API_PAGE)]))[0]
        cli = run_bundle(cli_hwp, dict(self.STEP_PAGE))[0]
        self.assertTrue(cli['ok'])
        cli_result = cli['steps'][0]['result']['result']
        self.assertEqual(_execute_shapes(api_hwp), _execute_shapes(cli_hwp))
        self.assertEqual([c.CtrlID for c in api_hwp.controls], [c.CtrlID for c in cli_hwp.controls])
        for key in ('changed', 'status', 'position', 'start_number', 'side_char', 'readback'):
            self.assertEqual(api[key], cli_result[key], msg=key)

    def test_page_number_requests_are_refused_identically_with_zero_native_calls(self) -> None:
        bad = (
            {'position': 'middle'},
            {'position': 'bottom-center', 'start_number': 0},
            {'position': 'bottom-center', 'start_number': True},
            {'position': 'bottom-center', 'prefix': '-'},
            {'position': 'bottom-center', 'prefix': '<', 'suffix': '>'},
            {'position': 'bottom-center', 'side_char': 'bold'},
        )
        for fields in bad:
            with self.subTest(fields=fields):
                with self.assertRaises(edit_ops.EditOperationError):
                    edit_ops.normalize_operations([{'op': 'page_number', **fields}])
                hwp = _hwpml()
                with self.assertRaises(Exception):
                    run_bundle(hwp, {**PAGE_STEP, **fields})
                self.assertEqual(hwp.calls, [])

    def test_protected_page_step_keeps_confirm_layout(self) -> None:
        hwp = _hwpml()
        with self.assertRaises(Exception):
            run_bundle(hwp, {**PAGE_STEP, 'confirm_layout': False})
        self.assertEqual(hwp.calls, [])

    def test_api_does_not_trim_find_so_padded_text_is_refused_while_the_step_trims(self) -> None:
        api_hwp, cli_hwp = _hwpml(), _hwpml()
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            edit_ops.apply_edit_operations(
                api_hwp, edit_ops.normalize_operations([{'op': 'official_numbering', 'find': '  Item text  ', 'level': 2}])
            )
        self.assertEqual(ctx.exception.code, 'target_not_found')
        self.assertEqual(executes(api_hwp), [])
        cli = run_bundle(cli_hwp, {**NUMBERING_STEP, 'match': '  Item text  '})[0]
        self.assertTrue(cli['ok'])
        self.assertEqual(headings(cli_hwp), [0, 1, 0])

    def test_match_case_is_an_api_only_option_and_the_step_is_always_case_sensitive(self) -> None:
        def recorded(hwp):
            seen: list[object] = []
            original = hwp.find

            def find(text, direction='Forward', **kwargs):
                seen.append(kwargs.get('MatchCase'))
                return original(text, direction, **kwargs)

            hwp.find = find
            return seen

        api_default, api_insensitive, cli = _hwpml(), _hwpml(), _hwpml()
        seen_default, seen_insensitive, seen_cli = recorded(api_default), recorded(api_insensitive), recorded(cli)
        edit_ops.apply_edit_operations(
            api_default, edit_ops.normalize_operations([{'op': 'official_numbering', 'find': 'Item text', 'level': 2}])
        )
        edit_ops.apply_edit_operations(
            api_insensitive,
            edit_ops.normalize_operations([{'op': 'official_numbering', 'find': 'Item text', 'level': 2, 'match_case': False}]),
        )
        self.assertTrue(run_bundle(cli, dict(NUMBERING_STEP))[0]['ok'])
        self.assertTrue(seen_default and set(seen_default) == {1})
        self.assertTrue(seen_insensitive and set(seen_insensitive) == {0})
        self.assertTrue(seen_cli and set(seen_cli) == {1})

    def test_real_compact_snapshot_neither_mutates_nor_changes_the_unknown_outcome(self) -> None:
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                results = []
                for real in (False, True):
                    hwp = _hwpml(foreign=foreign)
                    action = 'ParagraphShape'
                    hook_execute(hwp, returns={action: None})
                    result, persisted, _snapshots = run_bundle(hwp, NUMBERING_STEP, real_snapshot=real)
                    results.append((result, persisted, hwp))
                (stub_result, stub_persisted, stub_hwp), (real_result, real_persisted, real_hwp) = results
                self.assertFalse(real_result['ok'])
                self.assertTrue(real_result['dirty'])
                self.assertTrue(real_persisted['dirty'])
                real_step = real_result['steps'][0]
                self.assertTrue(real_step['mutation_may_have_persisted'])
                self.assertIn('before', real_step)
                self.assertIn('after', real_step)
                self.assertEqual(
                    [call[:2] for call in real_hwp.calls], [call[:2] for call in stub_hwp.calls], 'the compact snapshot adds no native call'
                )
                self.assertEqual(real_hwp.paragraphs, stub_hwp.paragraphs)

    def test_real_compact_snapshot_does_not_hide_a_successful_step(self) -> None:
        hwp = _hwpml(foreign=True)
        result, persisted, _snapshots = run_bundle(hwp, NUMBERING_STEP, real_snapshot=True)
        self.assertTrue(result['ok'])
        self.assertTrue(persisted['dirty'])
        self.assertEqual(headings(hwp), [0, 1, 0])


if __name__ == '__main__':
    unittest.main()
