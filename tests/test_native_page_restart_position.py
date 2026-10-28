"""F1/F2 repair tests for the page-restart (NEWNUM) evidence.

Real data: the XML snapshots are the genuine raw before/after pair behind ``tests.test_native_page_restart_evidence``.
Everything that drives them is SYNTHETIC (``SyntheticComHwp`` is a fake COM object; every mutated XML text is an invented
variant of the real pair). These are source/fake checks only, not native executions and not observed native corruption.

F1: a pre-existing page restart that moves beside its text siblings must not pass as an unchanged document.
F2: an unchanged page restart in the first paragraph whose number contradicts the requested start must not pass.
"""
from __future__ import annotations

import unittest
from typing import Any

from app import native_numbering as nn
from app.native_numbering_census import live_protection_reader, parse_hwpml2x_census
from tests import test_native_page_restart_evidence as base
from tests.test_native_numbering_bundle_translation import NO_CLEANUP, PAGE_STEP, _run_bundle

SyntheticComHwp = base.SyntheticComHwp
mutate = base.mutate
remove_newnum = base.remove_newnum
EXISTING = base.EXISTING
SECOND = base.SECOND_PARAGRAPH_CHAR
FIRST = '<CHAR>NQPAGEONE</CHAR>'
SPLIT = '</TEXT><TEXT CharShape="0"><CHAR>SPLIT</CHAR>'


def setUpModule() -> None:
    base.setUpModule()


def apply(hwp: SyntheticComHwp, request: dict[str, Any] | None = None, protection: Any = None) -> dict[str, Any]:
    return nn.apply_page_number(hwp, request or base.REQUEST, protection=protection or live_protection_reader(hwp))


def pair(before_marker: str, after_marker: str, anchor: str) -> tuple[str, str]:
    """Real before text and real after text (inserted restart removed) with the marker moved around the anchor."""
    return (
        mutate(base.BEFORE, anchor, before_marker),
        mutate(remove_newnum(base.AFTER), anchor, after_marker),
    )


class _Unknown(unittest.TestCase):
    def assert_apply_unknown(self, hwp: SyntheticComHwp, *, request: dict[str, Any] | None = None) -> None:
        with self.assertRaises(nn.NativeNumberingMutationError) as caught:
            apply(hwp, request)
        error = caught.exception
        self.assertEqual(error.mutation_state, 'unknown')
        self.assertTrue(error.mutation_may_have_persisted)
        self.assertEqual(error.rollback, NO_CLEANUP)
        self.assertEqual(sum(1 for call in hwp.calls if call[0] == 'Execute'), 1)
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)
        self.assertFalse([c for c in hwp.calls if c[0] in {'Run', 'Clear', 'Quit'}])


class RestartPositionObservationTests(unittest.TestCase):
    def test_every_observation_has_one_position_per_path_step_plus_itself(self) -> None:
        census = parse_hwpml2x_census(base.AFTER)
        self.assertEqual(len(census['page_restart_positions']), len(census['page_restarts']))
        for entry, position in zip(census['page_restarts'], census['page_restart_positions']):
            self.assertEqual(len(position), len(entry['path']) + 1)

    def test_before_pair_has_no_positions_and_original_observation_keys_are_unchanged(self) -> None:
        self.assertEqual(parse_hwpml2x_census(base.BEFORE)['page_restart_positions'], [])
        entry = parse_hwpml2x_census(base.AFTER)['page_restarts'][0]
        self.assertEqual(set(entry), {'location', 'section', 'paragraph', 'path', 'attrs', 'children', 'text', 'tail'})

    def test_position_moves_with_the_restart_beside_a_text_sibling(self) -> None:
        before, after = pair(EXISTING + SECOND, SECOND + EXISTING, SECOND)
        left, right = parse_hwpml2x_census(before), parse_hwpml2x_census(after)
        self.assertEqual(left['page_restarts'], right['page_restarts'])
        self.assertNotEqual(left['page_restart_positions'], right['page_restart_positions'])

    def test_position_moves_with_the_restart_into_a_sibling_text_element(self) -> None:
        before, after = pair(EXISTING + SECOND + SPLIT, SECOND + SPLIT.replace('<CHAR>', EXISTING + '<CHAR>'), SECOND)
        left, right = parse_hwpml2x_census(before), parse_hwpml2x_census(after)
        self.assertEqual(left['page_restarts'], right['page_restarts'])
        self.assertNotEqual(left['page_restart_positions'], right['page_restart_positions'])

    def test_page_number_elements_and_other_restarts_do_not_shift_the_position(self) -> None:
        plain = parse_hwpml2x_census(mutate(base.BEFORE, SECOND, EXISTING + SECOND))
        for label, lead in (
            ('page number element', base.PAGENUM),
            ('bare page restart', '<NEWNUM Number="9" NumberType="Page"/>'),
            ('wrapper holding only a page number element', '<CTRL>' + base.PAGENUM + '</CTRL>'),
        ):
            with self.subTest(label):
                shifted = parse_hwpml2x_census(mutate(base.BEFORE, SECOND, lead + EXISTING + SECOND))
                self.assertEqual(plain['page_restart_positions'][-1], shifted['page_restart_positions'][-1])

    def test_a_visible_sibling_does_shift_the_position(self) -> None:
        plain = parse_hwpml2x_census(mutate(base.BEFORE, SECOND, EXISTING + SECOND))
        shifted = parse_hwpml2x_census(mutate(base.BEFORE, SECOND, '<CHAR>X</CHAR>' + EXISTING + SECOND))
        self.assertNotEqual(plain['page_restart_positions'][-1], shifted['page_restart_positions'][-1])

    def test_malformed_or_inconsistent_positions_are_rejected_and_absent_positions_are_accepted(self) -> None:
        census = parse_hwpml2x_census(base.AFTER)
        for label, value in (
            ('wrong length', []),
            ('not a list', 'x'),
            ('boolean step', [[True, 0, 0, 0]]),
            ('non-integer step', [['0', 0, 0, 0]]),
        ):
            with self.subTest(label), self.assertRaises(RuntimeError):
                nn._validated_census(dict(census, page_restart_positions=value))
        with self.assertRaises(RuntimeError):
            nn._validated_census(dict(census, page_restarts=None))
        absent = {key: value for key, value in census.items() if key != 'page_restart_positions'}
        self.assertIsNone(nn._validated_census(absent)['page_restart_positions'])


class F1ExistingRestartPositionTests(_Unknown):
    def test_restart_moved_within_the_same_paragraph_is_unknown(self) -> None:
        before, after = pair(EXISTING + SECOND, SECOND + EXISTING, SECOND)
        for new_number in ('echo', None):
            with self.subTest(new_number=new_number):
                self.assert_apply_unknown(SyntheticComHwp(before, after, new_number=new_number))

    def test_restart_moved_into_a_sibling_text_element_is_unknown(self) -> None:
        before, after = pair(EXISTING + SECOND + SPLIT, SECOND + SPLIT.replace('<CHAR>', EXISTING + '<CHAR>'), SECOND)
        self.assert_apply_unknown(SyntheticComHwp(before, after))

    def test_restart_moved_across_a_paragraph_boundary_stays_unknown(self) -> None:
        before = mutate(base.BEFORE, SECOND, EXISTING + SECOND)
        after = mutate(remove_newnum(base.AFTER), '<CHAR>NQLEVELTHREE</CHAR>', EXISTING + '<CHAR>NQLEVELTHREE</CHAR>')
        self.assert_apply_unknown(SyntheticComHwp(before, after))

    def test_unchanged_positions_stay_supported_before_and_after_the_text(self) -> None:
        for label, marker in (('before the text', EXISTING + SECOND), ('after the text', SECOND + EXISTING)):
            with self.subTest(label):
                before, after = pair(marker, marker, SECOND)
                self.assertEqual(apply(SyntheticComHwp(before, after))['status'], 'inserted')

    def test_unchanged_restart_inside_a_second_text_element_stays_supported(self) -> None:
        marker = SECOND + SPLIT.replace('<CHAR>', EXISTING + '<CHAR>')
        before, after = pair(marker, marker, SECOND)
        self.assertEqual(apply(SyntheticComHwp(before, after))['status'], 'inserted')

    def test_public_bundle_action_reports_unknown_without_success_or_rollback(self) -> None:
        before, after = pair(EXISTING + SECOND, SECOND + EXISTING, SECOND)
        hwp = SyntheticComHwp(before, after)
        result, persisted = _run_bundle(hwp, dict(PAGE_STEP, start_number=7))
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(persisted['dirty'])
        step = result['steps'][0]
        self.assertFalse(step['ok'])
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertEqual(step['rollback'], NO_CLEANUP)

    def test_census_without_positions_does_not_vouch_for_the_real_pair(self) -> None:
        hwp = SyntheticComHwp()
        reader = live_protection_reader(hwp)

        def without_positions() -> dict[str, Any]:
            return {key: value for key, value in reader().items() if key != 'page_restart_positions'}

        with self.assertRaises(nn.NativeNumberingMutationError):
            apply(hwp, protection=without_positions)


class F2FirstParagraphStartTests(_Unknown):
    def test_unchanged_first_paragraph_restart_contradicting_the_start_is_unknown(self) -> None:
        before, after = pair(EXISTING + FIRST, EXISTING + FIRST, FIRST)
        for new_number in ('echo', None):
            with self.subTest(new_number=new_number):
                self.assert_apply_unknown(SyntheticComHwp(before, after, new_number=new_number))

    def test_the_conflict_is_named_in_the_refusal(self) -> None:
        before, after = pair(EXISTING + FIRST, EXISTING + FIRST, FIRST)
        with self.assertRaises(nn.NativeNumberingMutationError) as caught:
            apply(SyntheticComHwp(before, after))
        self.assertIn('contradicts the requested start 7', str(caught.exception))

    def test_unreadable_or_signed_numbers_in_the_first_paragraph_are_unknown(self) -> None:
        for number in ('abc', '', '-3', '3.0', '+7'):
            with self.subTest(number=number):
                marker = f'<NEWNUM Number="{number}" NumberType="Page"/>' + FIRST
                before, after = pair(marker, marker, FIRST)
                self.assert_apply_unknown(SyntheticComHwp(before, after))

    def test_restart_after_the_first_character_is_equally_a_first_paragraph_restart(self) -> None:
        before, after = pair(FIRST + EXISTING, FIRST + EXISTING, FIRST)
        self.assert_apply_unknown(SyntheticComHwp(before, after))

    def test_first_paragraph_restart_that_agrees_with_the_start_stays_supported(self) -> None:
        for number, start in (('7', 7), ('07', 7), ('3', 3)):
            with self.subTest(number=number, start=start):
                marker = f'<NEWNUM Number="{number}" NumberType="Page"/>' + FIRST
                before, after = pair(marker, marker, FIRST)
                result = apply(SyntheticComHwp(before, after), request={'position': 'bottom-center', 'start_number': start})
                self.assertEqual(result['status'], 'inserted')

    def test_unrelated_second_paragraph_restart_stays_supported_with_a_different_number(self) -> None:
        before, after = pair(EXISTING + SECOND, EXISTING + SECOND, SECOND)
        self.assertEqual(apply(SyntheticComHwp(before, after))['status'], 'inserted')

    def test_public_bundle_action_reports_unknown_without_success_or_rollback(self) -> None:
        before, after = pair(EXISTING + FIRST, EXISTING + FIRST, FIRST)
        hwp = SyntheticComHwp(before, after)
        result, persisted = _run_bundle(hwp, dict(PAGE_STEP, start_number=7))
        self.assertFalse(result['ok'])
        self.assertTrue(persisted['dirty'])
        step = result['steps'][0]
        self.assertFalse(step['ok'])
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertEqual(step['rollback'], NO_CLEANUP)

    def test_real_pair_and_none_readback_proof_keep_working(self) -> None:
        self.assertEqual(apply(SyntheticComHwp())['status'], 'inserted')
        result = apply(SyntheticComHwp(new_number=None))
        self.assertEqual(result['status'], 'inserted')
        self.assertEqual(result['readback']['new_number_source'], 'hwpml2x_page_restart')


if __name__ == '__main__':
    unittest.main()
