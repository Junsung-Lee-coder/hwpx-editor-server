"""Page 1 as Hancom writes it: PageNumPos with start 1 adds PAGENUM and no NEWNUM, and the printed first number comes from the document.

The XML is the real captured pair with the restart removed, which is what Hancom 13.0.0.3457 was measured to produce for start 1. The
COM object is synthetic, so this is the product's proof over real XML, not a native execution; the native run is recorded separately.
"""
from __future__ import annotations

import unittest

from app import native_numbering as nn
from app.native_numbering_census import live_protection_reader, parse_hwpml2x_census
from tests.native_numbering_fakes import FakeCtrl
from tests import test_native_page_restart_evidence as pair
from tests.test_native_numbering_bundle_translation import PAGE_STEP, _run_bundle
from tests.test_native_page_restart_evidence import EXISTING, NEWNUM, SECOND_PARAGRAPH_CHAR, SyntheticComHwp, mutate, remove_newnum

SECTION_START = 'Page="0" PageStartsOn'
FIRST_CHAR = '<CHAR>NQPAGEONE</CHAR>'


def setUpModule() -> None:
    pair.setUpModule()


def texts() -> tuple[str, str]:
    """The measured document before and after PageNumPos with start 1."""
    return pair.BEFORE, remove_newnum(pair.AFTER)


def restart(number: int) -> str:
    return f'<NEWNUM Number="{number}" NumberType="Page"/>'


def with_first_restart(text: str, number: int) -> str:
    return mutate(text, FIRST_CHAR, restart(number) + FIRST_CHAR)


def hwp_for(before: str, after: str, **options) -> SyntheticComHwp:
    return SyntheticComHwp(before, after, new_number=None, **options)


def apply(hwp: SyntheticComHwp, **request) -> dict:
    return nn.apply_page_number(hwp, {'position': 'bottom-center', **request}, protection=live_protection_reader(hwp))


def executes(hwp: SyntheticComHwp) -> list:
    return [call for call in hwp.calls if call[0] == 'Execute']


class ImplicitPageOne(unittest.TestCase):
    def test_omitted_and_explicit_start_one_are_inserted_and_say_how_it_was_proven(self) -> None:
        for request in ({}, {'start_number': 1}):
            with self.subTest(request=request):
                hwp = hwp_for(*texts())
                result = apply(hwp, **request)
                self.assertEqual((result['status'], result['changed'], result['start_number']), ('inserted', True, 1))
                self.assertEqual(result['readback']['values']['NewNumber'], 1)
                self.assertEqual(result['readback']['new_number_source'], 'implicit_first_page')
                self.assertEqual(result['protection']['page_number_proof'], 'control_chain+implicit_first_page')
                self.assertEqual(len(executes(hwp)), 1)

    def test_the_public_bundle_is_not_held_and_marks_the_document_dirty(self) -> None:
        for step in ({k: v for k, v in PAGE_STEP.items() if k != 'start_number'}, dict(PAGE_STEP, start_number=1)):
            with self.subTest(step=step.get('start_number')):
                hwp = hwp_for(*texts())
                result, _persisted = _run_bundle(hwp, step)
                self.assertTrue(result['ok'], result)
                self.assertTrue(result['dirty'])
                self.assertFalse(result.get('native_custody_held', False))
                self.assertEqual(result['steps'][0]['result']['result']['readback']['new_number_source'], 'implicit_first_page')

    def test_a_section_starting_at_one_is_the_same_implicit_page_one(self) -> None:
        before, after = texts()
        hwp = hwp_for(mutate(before, SECTION_START, 'Page="1" PageStartsOn'), mutate(after, SECTION_START, 'Page="1" PageStartsOn'))
        self.assertEqual(apply(hwp, start_number=1)['readback']['new_number_source'], 'implicit_first_page')

    def test_a_restart_hancom_did_write_for_start_one_is_proven_by_that_restart(self) -> None:
        before, _after = texts()
        hwp = hwp_for(before, mutate(pair.AFTER, NEWNUM, restart(1)))
        result = apply(hwp, start_number=1)
        self.assertEqual(result['readback']['new_number_source'], 'hwpml2x_page_restart')
        self.assertIn('hwpml2x_page_restart', result['protection']['page_number_proof'])


class WhatIsNotPageOne(unittest.TestCase):
    def assert_held(self, hwp: SyntheticComHwp, *, contains: str | None = None, **request) -> None:
        with self.assertRaises(nn.NativeNumberingMutationError) as caught:
            apply(hwp, **request)
        self.assertEqual(caught.exception.mutation_state, 'unknown')
        self.assertTrue(caught.exception.mutation_may_have_persisted)
        self.assertEqual(len(executes(hwp)), 1)
        if contains is not None:
            self.assertIn(contains, str(caught.exception))

    def test_a_missing_restart_does_not_prove_any_other_start(self) -> None:
        self.assert_held(hwp_for(*texts()), start_number=3, contains='no new page restart')

    def test_another_restart_written_for_start_one_is_not_page_one(self) -> None:
        before, _after = texts()
        self.assert_held(hwp_for(before, mutate(pair.AFTER, NEWNUM, restart(5))), start_number=1)

    def test_a_section_start_changed_by_the_action_is_not_page_one(self) -> None:
        before, after = texts()
        self.assert_held(hwp_for(before, mutate(after, SECTION_START, 'Page="5" PageStartsOn')), start_number=1, contains='section page start')

    def test_a_control_reading_another_number_is_not_page_one(self) -> None:
        before, after = texts()
        self.assert_held(SyntheticComHwp(before, after, new_number=3), start_number=1, contains='reads back')

    def test_text_changed_outside_the_page_number_is_still_held(self) -> None:
        before, after = texts()
        self.assert_held(hwp_for(before, mutate(after, SECOND_PARAGRAPH_CHAR, '<CHAR>NQLEVELXXX</CHAR>')), start_number=1)

    def test_an_extra_control_is_still_held(self) -> None:
        self.assert_held(hwp_for(*texts(), extra_control_after=True), start_number=1, contains='other controls')

    def test_a_section_starting_elsewhere_is_refused_before_any_edit(self) -> None:
        before, after = texts()
        hwp = hwp_for(mutate(before, SECTION_START, 'Page="5" PageStartsOn'), mutate(after, SECTION_START, 'Page="5" PageStartsOn'))
        with self.assertRaises(nn.NativeNumberingRefusal) as caught:
            apply(hwp, start_number=1)
        self.assertEqual(caught.exception.code, 'existing_section_page_start')
        self.assertEqual(executes(hwp), [])

    def test_a_section_starting_elsewhere_can_still_take_an_explicit_restart(self) -> None:
        moved = 'Page="5" PageStartsOn'
        hwp = hwp_for(mutate(pair.BEFORE, SECTION_START, moved), mutate(pair.AFTER, SECTION_START, moved))
        self.assertEqual(apply(hwp, start_number=7)['readback']['values']['NewNumber'], 7)


class ExistingRestart(unittest.TestCase):
    def refused(self, before: str, after: str, code: str = 'existing_page_restart', **request) -> None:
        hwp = hwp_for(before, after)
        with self.assertRaises(nn.NativeNumberingRefusal) as caught:
            apply(hwp, **request)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(executes(hwp), [], 'Hancom would rewrite the restart, and its Undo does not put it back')

    def test_a_first_paragraph_restart_with_another_number_is_refused_before_any_edit(self) -> None:
        before, after = texts()
        self.refused(with_first_restart(before, 3), with_first_restart(after, 1), start_number=1)
        self.refused(with_first_restart(before, 3), with_first_restart(after, 5), start_number=5)
        self.refused(with_first_restart(before, 1), with_first_restart(after, 3), start_number=3)

    def test_a_restart_later_in_the_document_is_refused_before_any_edit(self) -> None:
        before, after = texts()
        later = lambda text: mutate(text, SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)  # noqa: E731
        self.refused(later(before), later(after), start_number=1)
        self.refused(later(before), later(pair.AFTER), start_number=7)

    def test_a_first_paragraph_restart_with_the_requested_number_is_that_start(self) -> None:
        before, after = texts()
        for number in (1, 3):
            with self.subTest(number=number):
                hwp = hwp_for(with_first_restart(before, number), with_first_restart(after, number))
                result = apply(hwp, start_number=number)
                self.assertEqual(result['status'], 'inserted')
                self.assertEqual(result['readback']['values']['NewNumber'], number)
                self.assertEqual(result['readback']['new_number_source'], 'hwpml2x_page_restart')
                self.assertEqual(len(executes(hwp)), 1)

    def test_that_restart_changing_during_the_action_is_held(self) -> None:
        before, after = texts()
        hwp = hwp_for(with_first_restart(before, 3), with_first_restart(after, 4))
        with self.assertRaises(nn.NativeNumberingMutationError):
            apply(hwp, start_number=3)

    def test_a_clean_refusal_through_the_bundle_keeps_the_session_usable(self) -> None:
        before, after = texts()
        for name, first, last, code in (('restart', with_first_restart(before, 3), with_first_restart(after, 1), 'existing_page_restart'),
                                        ('section', mutate(before, SECTION_START, 'Page="5" PageStartsOn'), mutate(after, SECTION_START, 'Page="5" PageStartsOn'), 'existing_section_page_start')):
            with self.subTest(name=name):
                hwp = hwp_for(first, last)
                result, _persisted = _run_bundle(hwp, dict(PAGE_STEP, start_number=1))
                self.assertFalse(result['ok'])
                self.assertFalse(result.get('native_custody_held', False), result)
                self.assertFalse(result['dirty'])
                step = result['steps'][0]
                self.assertEqual(step['refusal_code'], code)
                self.assertFalse(step['mutation_may_have_persisted'])
                self.assertEqual(executes(hwp), [])
                self.assertIn('page', step['refusal_message'])

    def test_a_second_page_number_stays_refused(self) -> None:
        values = {'DrawPos': 5, 'NewNumber': None, 'SideChar': 45, 'NumberFormat': 0}
        from tests.native_numbering_fakes import FakeProps
        hwp = SyntheticComHwp(*texts(), new_number=None, controls=[FakeCtrl('secd'), FakeCtrl('pgnp', FakeProps(values))])
        with self.assertRaises(nn.NativeNumberingRefusal) as caught:
            apply(hwp, start_number=1, side_char='dash')
        self.assertEqual(caught.exception.code, 'existing_page_number')
        self.assertEqual(executes(hwp), [])


class TheReviewOfPageOne(unittest.TestCase):
    def test_a_page_number_with_no_restart_is_checked_against_one_on_the_print(self) -> None:
        from hwpx_mcp.review import rendered_page_numbers
        control = {'draw_pos': 5, 'side_char': 45}
        found = rendered_page_numbers({'page_numbers': [control], 'page_restarts': []}, ['본문\n- 1 -'], total_pages=1)
        self.assertEqual((found['state'], found['start_number'], found['all_found']), ('complete', 1, True))
        missing = rendered_page_numbers({'page_numbers': [control], 'page_restarts': []}, ['본문\n- 5 -'], total_pages=1)
        self.assertEqual((missing['state'], missing['all_found']), ('missing', False), 'a section printing another number is not called page 1')


class SectionStartEvidence(unittest.TestCase):
    def test_the_census_reads_the_section_page_start(self) -> None:
        self.assertEqual(parse_hwpml2x_census(pair.BEFORE)['section_page_starts'], [0])
        self.assertEqual(parse_hwpml2x_census(mutate(pair.BEFORE, SECTION_START, 'Page="5" PageStartsOn'))['section_page_starts'], [5])

    def test_an_unreadable_section_start_is_not_page_one(self) -> None:
        for bad in ('Page="x" PageStartsOn', 'PageStartsOn'):
            with self.subTest(bad=bad):
                before, after = texts()
                hwp = hwp_for(mutate(before, SECTION_START, bad), mutate(after, SECTION_START, bad))
                with self.assertRaises(nn.NativeNumberingRefusal) as caught:
                    apply(hwp, start_number=1)
                self.assertEqual(caught.exception.code, 'preservation_unprovable')
                self.assertEqual(executes(hwp), [])


if __name__ == '__main__':
    unittest.main()
