"""Page-restart (NEWNUM) evidence handling for the page number insertion.

Real data: the XML snapshots are the genuine raw before/after pair captured from the desktop editor around one
PageNumPos insertion (pinned by sha256 below). Everything that drives them is SYNTHETIC: ``SyntheticComHwp`` is a
fake COM object that serves the real before text until ``Execute('PageNumPos')`` and the real after text afterwards.
These tests therefore exercise the product parser, comparator and public action against real XML, but they are NOT
native executions and prove nothing about the saved/reopened start or about what ``Properties.Item('NewNumber')``
returns for any control other than the one real observation (None).
"""
from __future__ import annotations

import hashlib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app import native_numbering as nn
from app.native_numbering_census import live_protection_reader, parse_hwpml2x_census
from tests.native_numbering_fakes import FakeCtrl, FakeHwp, FakeProps, _FakeHAction
from tests.test_native_numbering_bundle_translation import NO_CLEANUP, PAGE_STEP, _run_bundle

PAIR_DIR = Path(__file__).resolve().parent / 'fixtures' / 'page_restart'
BEFORE_SHA = '6050d331e3b5bec3952efab0154d294b542f318c8a55b61a4c42537dcf3c2ab3'
AFTER_SHA = '4040bd6349dae97e8a6f75f9d11cbee001dda19545978eaf67bbb3f148956a86'
NEWNUM = '<NEWNUM Number="7" NumberType="Page"/>'
PAGENUM = '<PAGENUM FormatType="Digit" Pos="BottomCenter"/>'
EXISTING = '<NEWNUM Number="3" NumberType="Page"/>'
SECOND_PARAGRAPH_CHAR = '<CHAR>NQLEVELTWO</CHAR>'
REQUEST = {'position': 'bottom-center', 'start_number': 7}

BEFORE = ''
AFTER = ''


def setUpModule() -> None:
    global BEFORE, AFTER
    before_raw = (PAIR_DIR / 'before.hwpml2x').read_bytes()
    after_raw = (PAIR_DIR / 'after.hwpml2x').read_bytes()
    assert hashlib.sha256(before_raw).hexdigest() == BEFORE_SHA, 'before pair bytes changed'
    assert hashlib.sha256(after_raw).hexdigest() == AFTER_SHA, 'after pair bytes changed'
    assert (len(before_raw), len(after_raw)) == (29149, 29235)
    BEFORE = before_raw.decode('utf-8')
    AFTER = after_raw.decode('utf-8')
    assert AFTER.count(NEWNUM) == 1 and AFTER.count(PAGENUM) == 1 and BEFORE.count('NEWNUM') == 0


def mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def remove_newnum(text: str) -> str:
    return mutate(text, NEWNUM, '')


class _Action(_FakeHAction):
    def Execute(self, name: str, hset: Any) -> Any:  # noqa: N802 - mirrors the COM member name
        result = super().Execute(name, hset)
        if name == 'PageNumPos':
            owner = self.owner
            owner.executed = True
            if owner.new_number != 'echo':
                ctrl = owner.controls[-1]
                values = dict(ctrl.Properties._values)
                values['NewNumber'] = owner.new_number
                ctrl.Properties = _NewNumberProps(values)
            if owner.extra_control_after:
                owner.controls.append(FakeCtrl('tbl'))
        return result


class _NewNumberProps(FakeProps):
    def Item(self, name: str) -> Any:  # noqa: N802
        value = self._values[name]
        if name == 'NewNumber' and isinstance(value, Exception):
            raise value
        return value


class SyntheticComHwp(FakeHwp):
    """SYNTHETIC COM serving real XML snapshots; not a native execution."""

    def __init__(
        self,
        before: str | None = None,
        after: str | None = None,
        *,
        new_number: Any = 'echo',
        extra_control_after: bool = False,
        controls: list[FakeCtrl] | None = None,
    ) -> None:
        texts = ['NQLEVELONE', 'NQLEVELTWO', 'NQLEVELTHREE', 'NQLEVELFOUR', 'NQPAGETWO', 'NQPAGETHREE', 'tail']
        super().__init__(
            paragraphs=[{'text': text, 'heading_type': 0, 'level': 0} for text in texts],
            controls=controls or [FakeCtrl('secd')],
        )
        self.before_text = BEFORE if before is None else before
        self.after_text = AFTER if after is None else after
        self.new_number = new_number
        self.extra_control_after = extra_control_after
        self.executed = False
        self.HAction = _Action(self)

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
        if fmt == 'HWPML2X':
            return self.after_text if self.executed else self.before_text
        return super().get_text_file(fmt, option)


def apply(hwp: SyntheticComHwp, request: dict[str, Any] | None = None) -> dict[str, Any]:
    return nn.apply_page_number(hwp, request or REQUEST, protection=live_protection_reader(hwp))


class RealPairParserTests(unittest.TestCase):
    def test_pinned_pair_raw_bytes_need_the_str_the_product_passes(self) -> None:
        raw = (PAIR_DIR / 'after.hwpml2x').read_bytes()
        with self.assertRaises(ET.ParseError):
            ET.fromstring(raw)
        parse_hwpml2x_census(raw.decode('utf-8'))

    def test_page_restart_observations_of_the_real_pair(self) -> None:
        before, after = parse_hwpml2x_census(BEFORE), parse_hwpml2x_census(AFTER)
        self.assertEqual(before['page_restarts'], [])
        self.assertEqual(
            after['page_restarts'],
            [
                {
                    'location': 'body',
                    'section': 0,
                    'paragraph': 0,
                    'path': ['section', 'p', 'text'],
                    'attrs': [['number', '7'], ['numbertype', 'Page']],
                    'children': 0,
                    'text': '',
                    'tail': '',
                }
            ],
        )

    def test_old_stripped_digest_differs_but_restart_stripped_digest_matches_for_the_real_pair(self) -> None:
        before, after = parse_hwpml2x_census(BEFORE), parse_hwpml2x_census(AFTER)
        self.assertNotEqual(before['paragraphs'][0]['stripped_sha256'], after['paragraphs'][0]['stripped_sha256'])
        self.assertEqual(before['frame']['stripped_sha256'], after['frame']['stripped_sha256'])
        self.assertEqual(before['paragraphs'][0]['restart_stripped_sha256'], after['paragraphs'][0]['restart_stripped_sha256'])
        self.assertEqual(
            [p['restart_stripped_sha256'] for p in before['paragraphs']],
            [p['restart_stripped_sha256'] for p in after['paragraphs']],
        )
        self.assertNotEqual(before['paragraphs'][0]['sha256'], after['paragraphs'][0]['sha256'])

    def test_non_page_or_unknown_resets_stay_in_the_restart_stripped_digest(self) -> None:
        before = parse_hwpml2x_census(BEFORE)
        for label, text in (
            ('footnote', mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Footnote"/>')),
            ('equation', mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Equation"/>')),
            ('missing type', mutate(AFTER, NEWNUM, '<NEWNUM Number="7"/>')),
            ('child', mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Page"><X/></NEWNUM>')),
            ('text', mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Page">x</NEWNUM>')),
            ('extra attribute', mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Page" Extra="1"/>')),
        ):
            with self.subTest(label):
                census = parse_hwpml2x_census(text)
                self.assertNotEqual(
                    before['paragraphs'][0]['restart_stripped_sha256'], census['paragraphs'][0]['restart_stripped_sha256']
                )


class _Assertions(unittest.TestCase):
    def assert_unknown(self, error: nn.NativeNumberingMutationError, hwp: SyntheticComHwp, *, contains: str | None = None) -> None:
        self.assertEqual(error.mutation_state, 'unknown')
        self.assertTrue(error.mutation_may_have_persisted)
        self.assertEqual(error.rollback, NO_CLEANUP)
        self.assertEqual(sum(1 for call in hwp.calls if call[0] == 'Execute'), 1)
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1, 'the inserted control is never cleaned up')
        self.assertFalse([c for c in hwp.calls if c[0] == 'Run'])
        if contains is not None:
            self.assertIn(contains, str(error))

    def assert_apply_unknown(self, hwp: SyntheticComHwp, *, request: dict[str, Any] | None = None, contains: str | None = None) -> None:
        with self.assertRaises(nn.NativeNumberingMutationError) as caught:
            apply(hwp, request)
        self.assert_unknown(caught.exception, hwp, contains=contains)


class RequestedStartPositiveTests(_Assertions):
    def test_integer_readback_with_the_real_pair_is_inserted_and_protected(self) -> None:
        hwp = SyntheticComHwp()
        result = apply(hwp)
        self.assertEqual(result['status'], 'inserted')
        self.assertTrue(result['changed'])
        self.assertEqual(result['readback']['values']['NewNumber'], 7)
        self.assertTrue(result['protection']['checked'])
        self.assertEqual(result['protection']['unproven'], [])
        self.assertEqual(sum(1 for call in hwp.calls if call[0] == 'Execute'), 1)

    def test_none_readback_is_completed_only_from_the_matching_explicit_xml_restart(self) -> None:
        hwp = SyntheticComHwp(new_number=None)
        result = apply(hwp)
        self.assertEqual(result['status'], 'inserted')
        self.assertEqual(result['readback']['values']['NewNumber'], 7)
        self.assertEqual(result['readback']['new_number_source'], 'hwpml2x_page_restart')
        self.assertIn('hwpml2x_page_restart', result['protection']['page_number_proof'])
        self.assertTrue(result['protection']['checked'])
        self.assertEqual(sum(1 for call in hwp.calls if call[0] == 'Execute'), 1)

    def test_public_bundle_action_inserts_with_integer_and_none_readback(self) -> None:
        for new_number in ('echo', None):
            with self.subTest(new_number=new_number):
                hwp = SyntheticComHwp(new_number=new_number)
                result, persisted = _run_bundle(hwp, dict(PAGE_STEP, start_number=7))
                self.assertTrue(result['ok'], result)
                self.assertTrue(result['dirty'])
                self.assertEqual(result['steps'][0]['result']['result']['status'], 'inserted')
                self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)

    def test_unchanged_unrelated_existing_page_restart_does_not_block_an_integer_readback_insertion(self) -> None:
        before = mutate(BEFORE, SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)
        after = mutate(remove_newnum(AFTER), SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)
        result = apply(SyntheticComHwp(before, after))
        self.assertEqual(result['status'], 'inserted')

    def test_old_format_census_with_integer_readback_keeps_working(self) -> None:
        hwp = SyntheticComHwp()
        result = nn.apply_page_number(hwp, REQUEST, protection=hwp.census)
        self.assertEqual(result['status'], 'inserted')
        self.assertEqual(result['protection']['page_number_proof'], 'control_chain')


class RestartEvidenceNegativeTests(_Assertions):
    def test_mutated_after_snapshots_are_unknown_for_both_readbacks(self) -> None:
        cases = {
            'number 8': mutate(AFTER, NEWNUM, '<NEWNUM Number="8" NumberType="Page"/>'),
            'number 07': mutate(AFTER, NEWNUM, '<NEWNUM Number="07" NumberType="Page"/>'),
            'footnote reset': mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Footnote"/>'),
            'equation reset': mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Equation"/>'),
            'figure reset': mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Figure"/>'),
            'missing type': mutate(AFTER, NEWNUM, '<NEWNUM Number="7"/>'),
            'duplicate': mutate(AFTER, NEWNUM, NEWNUM + NEWNUM),
            'extra attribute': mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Page" Extra="1"/>'),
            'child content': mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Page"><X/></NEWNUM>'),
            'text content': mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Page">x</NEWNUM>'),
            'relocated to paragraph 1': mutate(remove_newnum(AFTER), SECOND_PARAGRAPH_CHAR, NEWNUM + SECOND_PARAGRAPH_CHAR),
            'directly under the section': mutate(
                remove_newnum(AFTER),
                '<P ParaShape="0" Style="0"><TEXT CharShape="0">' + SECOND_PARAGRAPH_CHAR,
                NEWNUM + '<P ParaShape="0" Style="0"><TEXT CharShape="0">' + SECOND_PARAGRAPH_CHAR,
            ),
            'control wrapper':mutate(AFTER, NEWNUM, '<CTRL>' + NEWNUM + '</CTRL>'),
            'foreign second page restart': mutate(AFTER, SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR),
        }
        for label, after in cases.items():
            for new_number in ('echo', None):
                with self.subTest(label=label, new_number=new_number):
                    self.assert_apply_unknown(SyntheticComHwp(after=after, new_number=new_number))

    def test_request_mismatch_is_unknown_for_both_readbacks(self) -> None:
        for new_number in ('echo', None):
            with self.subTest(new_number=new_number):
                self.assert_apply_unknown(SyntheticComHwp(new_number=new_number), request={'position': 'bottom-center', 'start_number': 8})

    def test_missing_restart_with_none_readback_is_unknown(self) -> None:
        self.assert_apply_unknown(SyntheticComHwp(after=remove_newnum(AFTER), new_number=None))

    def test_start_one_without_restart_and_none_readback_is_not_inferred_as_one(self) -> None:
        after = remove_newnum(AFTER)
        self.assert_apply_unknown(SyntheticComHwp(after=after, new_number=None), request={'position': 'bottom-center', 'start_number': 1})

    def test_none_readback_without_a_protection_reader_or_with_old_evidence_is_unknown(self) -> None:
        hwp = SyntheticComHwp(new_number=None)
        with self.assertRaises(nn.NativeNumberingMutationError) as caught:
            nn.apply_page_number(hwp, REQUEST)
        self.assert_unknown(caught.exception, hwp)
        hwp = SyntheticComHwp(new_number=None)
        with self.assertRaises(nn.NativeNumberingMutationError) as caught:
            nn.apply_page_number(hwp, REQUEST, protection=hwp.census)
        self.assert_unknown(caught.exception, hwp)

    def test_preexisting_page_restart_is_ambiguous_for_both_readbacks_when_it_changes(self) -> None:
        before = mutate(BEFORE, SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)
        after = mutate(AFTER, SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)
        for new_number in ('echo', None):
            with self.subTest(new_number=new_number):
                self.assert_apply_unknown(SyntheticComHwp(before, after, new_number=new_number))

    def test_unchanged_preexisting_restart_cannot_prove_a_none_readback(self) -> None:
        before = mutate(BEFORE, SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)
        after = mutate(remove_newnum(AFTER), SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)
        self.assert_apply_unknown(SyntheticComHwp(before, after, new_number=None))

    def test_preexisting_restart_removed_or_altered_is_unknown(self) -> None:
        before = mutate(BEFORE, SECOND_PARAGRAPH_CHAR, EXISTING + SECOND_PARAGRAPH_CHAR)
        for new_number in ('echo', None):
            with self.subTest(new_number=new_number):
                self.assert_apply_unknown(SyntheticComHwp(before, AFTER, new_number=new_number))

    def test_non_integer_new_number_readbacks_stay_unknown_even_when_the_xml_matches(self) -> None:
        for value in ('7', True, 7.0, RuntimeError('boom')):
            with self.subTest(value=repr(value)):
                self.assert_apply_unknown(SyntheticComHwp(new_number=value))

    def test_integer_readback_that_differs_from_the_request_stays_unknown(self) -> None:
        self.assert_apply_unknown(SyntheticComHwp(new_number=8))


class ProtectedStateNegativeTests(_Assertions):
    def test_protected_changes_beside_the_restart_are_unknown_for_both_readbacks(self) -> None:
        cases = {
            'paragraph text': mutate(AFTER, '<CHAR>NQLEVELTWO</CHAR>', '<CHAR>NQLEVELTWX</CHAR>'),
            'page definition': mutate(AFTER, 'Width="59528"', 'Width="59529"'),
            'style': mutate(AFTER, 'EngName="Body"', 'EngName="Bodx"'),
            'character shape': mutate(AFTER, 'Height="1600"', 'Height="1601"'),
            'media list': mutate(AFTER, '<BORDERFILLLIST Count="2">', '<BINDATALIST Count="1"><BINITEM Id="1"/></BINDATALIST><BORDERFILLLIST Count="2">'),
            'header content': mutate(AFTER, NEWNUM, NEWNUM + '<HEADER ApplyPageType="Both"><P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>H</CHAR></TEXT></P></HEADER>'),
        }
        for label, after in cases.items():
            for new_number in ('echo', None):
                with self.subTest(label=label, new_number=new_number):
                    self.assert_apply_unknown(SyntheticComHwp(after=after, new_number=new_number))

    def test_an_extra_control_after_the_call_is_unknown(self) -> None:
        for new_number in ('echo', None):
            with self.subTest(new_number=new_number):
                self.assert_apply_unknown(SyntheticComHwp(new_number=new_number, extra_control_after=True))

    def test_public_bundle_action_reports_unknown_without_success_or_rollback(self) -> None:
        for after in (mutate(AFTER, NEWNUM, '<NEWNUM Number="8" NumberType="Page"/>'), mutate(AFTER, NEWNUM, '<NEWNUM Number="7" NumberType="Footnote"/>')):
            for new_number in ('echo', None):
                with self.subTest(new_number=new_number):
                    hwp = SyntheticComHwp(after=after, new_number=new_number)
                    result, persisted = _run_bundle(hwp, dict(PAGE_STEP, start_number=7))
                    self.assertFalse(result['ok'])
                    self.assertTrue(result['dirty'])
                    self.assertTrue(persisted['dirty'])
                    step = result['steps'][0]
                    self.assertFalse(step['ok'])
                    self.assertTrue(step['mutation_may_have_persisted'])
                    self.assertEqual(step['rollback'], NO_CLEANUP)
                    self.assertNotIn('inserted', step['error'].split(':', 1)[0])
                    self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)


class ExistingControlRefusalTests(unittest.TestCase):
    def test_identical_noop_with_an_unset_new_number_stays_refused_before_any_mutation(self) -> None:
        values = {'DrawPos': 5, 'NewNumber': None, 'SideChar': 0, 'NumberFormat': 0}
        hwp = SyntheticComHwp(controls=[FakeCtrl('secd'), FakeCtrl('pgnp', FakeProps(values))])
        with self.assertRaises(nn.NativeNumberingRefusal) as caught:
            apply(hwp)
        self.assertEqual(caught.exception.code, 'existing_page_number')
        self.assertEqual([c for c in hwp.calls if c[0] in {'Execute', 'Run'}], [])


if __name__ == '__main__':
    unittest.main()
