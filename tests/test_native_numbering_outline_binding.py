"""A section outline binding that is missing or unreadable must leave NUMBER sharing unknown, never separate.

A NUMBER paragraph may use the numbering definition that a section's SECDEF OutlineShape names. The census can only say
"separate" when every BODY SECTION has a readable OutlineShape binding; a SECDEF without OutlineShape, a section without any
SECDEF, a blank or dangling reference all leave the answer None. A reference that is already named by an observed binding
stays True whatever the other sections look like. The consumer then refuses a definition change before the first mutator.

The HWPML2X samples and the SECDEF/HeadingIdRef serialization are SYNTHETIC; nothing here says what Hancom does for a
section that has no OutlineShape. These tests exercise the code flow of this source against fakes only.
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from typing import Any

from app import native_numbering as nn
from app import native_numbering_census as census_module
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from tests.native_numbering_fakes import FakeCtrl
from tests.test_native_numbering_census import hwpml
from tests.test_native_numbering_outline_scope import mutating_calls, state
from tests.test_native_numbering_repair import NUMBERING_STEP, POSITION, _Service
from tests.test_native_numbering_review_contract import doc, executes, headings, run_official
from tests.test_native_numbering_shared_outline import _SharedHwp

UNPROVABLE = 'outline_usage_unprovable'
SHARED = 'foreign_outline_definition'
EXTRA_NUMBERINGS = ('(^9)', '(^8)')


def restructure(text: str, layout: list[tuple[int, list[str | None]]] | None) -> str:
    """Rebuild BODY as the given SECTIONs: (paragraph_count, SECDEFs) where a SECDEF is its OutlineShape ref or None for no attribute.

    layout None keeps the paragraphs directly under BODY with their original SECDEF.
    """
    root = ET.fromstring(text)
    body = root.find('BODY')
    paragraphs = [paragraph for section in list(body) for paragraph in list(section)]
    for section in list(body):
        body.remove(section)
    if layout is None:
        body.extend(paragraphs)
        return ET.tostring(root, encoding='unicode')
    assert sum(count for count, _ in layout) == len(paragraphs)
    for paragraph in paragraphs:
        for container in paragraph.iter('TEXT'):
            for secdef in container.findall('SECDEF'):
                container.remove(secdef)
    start = 0
    for index, (count, secdefs) in enumerate(layout):
        section = ET.SubElement(body, 'SECTION', {'Id': str(index)})
        chunk = paragraphs[start : start + count]
        start += count
        section.extend(chunk)
        for position, ref in enumerate(secdefs):
            chunk[0].find('TEXT').insert(position, ET.Element('SECDEF', {} if ref is None else {'OutlineShape': ref}))
    root.find('HEAD').set('SecCnt', str(len(layout)))
    return ET.tostring(root, encoding='unicode')


def refs_by_level(text: str, refs: dict[str, str]) -> str:
    root = ET.fromstring(text)
    for shape in root.iter('PARASHAPE'):
        if shape.get('HeadingType') == 'Number':
            shape.set('HeadingIdRef', refs[shape.get('Level')])
    return ET.tostring(root, encoding='unicode')


def sectioned(base: type, layout: list[tuple[int, list[str | None]]] | None) -> type:
    class Sectioned(base):  # type: ignore[misc, valid-type]
        def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
            text = super().get_text_file(fmt, option)
            return restructure(text, layout) if fmt == 'HWPML2X' else text

    return Sectioned


def census_of(layout: list[tuple[int, list[str | None]]] | None, refs: dict[str, str]) -> dict[str, Any]:
    """Census of Title, Item, a NUMBER paragraph at level 0 (refs['0']) and one at level 1 (refs['1'])."""
    paras = [('Title', 0, 0), ('Item', 0, 0), ('Num A', 2, 0), ('Num B', 2, 1)]
    return census_module.parse_hwpml2x_census(restructure(refs_by_level(hwpml(paras, extra_numberings=EXTRA_NUMBERINGS), refs), layout))


def flags(layout: list[tuple[int, list[str | None]]] | None, refs: dict[str, str]) -> list[Any]:
    return [item['shares_outline_numbering'] for item in census_of(layout, refs)['paragraphs']]


def bound_doc(layout: list[tuple[int, list[str | None]]] | None, number_ref: str, *, foreign: bool = True, numbers: tuple[int, ...] = (2,)) -> Any:
    hwp = doc(foreign=foreign, texts=('Title', 'Item text', 'Numbered', 'Next'), cls=sectioned(_SharedHwp, layout), controls=[FakeCtrl('secd')])
    for index in numbers:
        hwp.paragraphs[index]['heading_type'] = 2
    hwp.hwpml_extras = {'extra_numberings': EXTRA_NUMBERINGS}
    hwp.number_ref = number_ref
    return hwp


ONE_BOUND = [(4, ['1'])]
ONE_MISSING_SHAPE = [(4, [None])]
ONE_NO_SECDEF = [(4, [])]
S0_BOUND_S1_NO_SECDEF = [(3, ['1']), (1, [])]
S0_BOUND_S1_NO_SHAPE = [(3, ['1']), (1, [None])]
BOTH_BOUND = [(3, ['1']), (1, ['3'])]


class CensusTriStateTests(unittest.TestCase):
    def test_a_complete_binding_keeps_the_boolean_answers(self) -> None:
        self.assertEqual(flags(ONE_BOUND, {'0': '1', '1': '2'}), [False, False, True, False])
        self.assertEqual(flags(ONE_BOUND, {'0': '2', '1': '3'}), [False, False, False, False])

    def test_a_secdef_without_outline_shape_leaves_sharing_unknown(self) -> None:
        for ref in ('1', '2'):
            census = census_of(ONE_MISSING_SHAPE, {'0': ref, '1': ref})
            self.assertEqual([p['shares_outline_numbering'] for p in census['paragraphs']], [False, False, None, None], msg=ref)
            self.assertEqual(census['outline_shapes'], ['none'])

    def test_a_section_without_any_secdef_leaves_sharing_unknown(self) -> None:
        for ref in ('1', '2'):
            census = census_of(ONE_NO_SECDEF, {'0': ref, '1': ref})
            self.assertEqual([p['shares_outline_numbering'] for p in census['paragraphs']], [False, False, None, None], msg=ref)
            self.assertEqual(census['outline_shapes'], [])

    def test_paragraphs_directly_under_body_are_not_a_proven_section_binding(self) -> None:
        self.assertEqual(flags(None, {'0': '1', '1': '2'}), [False, False, True, None])

    def test_blank_and_dangling_outline_references_are_not_a_binding(self) -> None:
        self.assertEqual(flags([(4, ['  '])], {'0': '2', '1': '3'}), [False, False, None, None])
        self.assertEqual(flags([(4, ['77'])], {'0': '2', '1': '3'}), [False, False, None, None])

    def test_a_secdef_beside_one_without_a_shape_in_the_same_section_is_incomplete(self) -> None:
        self.assertEqual(flags([(4, ['1', None])], {'0': '2', '1': '1'}), [False, False, None, True])

    def test_known_shared_stays_true_when_another_section_is_unbound(self) -> None:
        for layout in (S0_BOUND_S1_NO_SECDEF, S0_BOUND_S1_NO_SHAPE):
            self.assertEqual(flags(layout, {'0': '1', '1': '2'}), [False, False, True, None], msg=layout)
            self.assertEqual(flags(layout, {'0': '2', '1': '1'}), [False, False, None, True], msg=layout)
            self.assertEqual(flags(layout, {'0': '2', '1': '3'}), [False, False, None, None], msg=layout)

    def test_a_reference_named_by_the_only_bound_later_section_is_true_and_the_unbound_first_section_is_unknown(self) -> None:
        self.assertEqual(flags([(3, []), (1, ['3'])], {'0': '2', '1': '3'}), [False, False, None, True])

    def test_every_section_bound_gives_known_answers_in_each_section(self) -> None:
        self.assertEqual(flags(BOTH_BOUND, {'0': '2', '1': '2'}), [False, False, False, False])
        self.assertEqual(flags(BOTH_BOUND, {'0': '2', '1': '3'}), [False, False, False, True])
        self.assertEqual(flags(BOTH_BOUND, {'0': '1', '1': '3'}), [False, False, True, True])
        self.assertEqual(flags(BOTH_BOUND, {'0': '3', '1': '1'}), [False, False, True, True])

    def test_the_numbering_validator_accepts_the_unknown_answer(self) -> None:
        census = nn._validated_census(census_of(ONE_MISSING_SHAPE, {'0': '2', '1': '2'}))  # noqa: SLF001
        self.assertIsNone(census['paragraphs'][2]['shares_outline_numbering'])


class BindingGateThroughPublicCallerTests(unittest.TestCase):
    def assert_refused_untouched(self, hwp: Any, code: str) -> None:
        before = state(hwp)
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual(executes(hwp), [])
        self.assertEqual(state(hwp), before)

    def test_missing_outline_shape_refuses_before_any_mutator_for_either_reference(self) -> None:
        for ref in ('1', '2'):
            self.assert_refused_untouched(bound_doc(ONE_MISSING_SHAPE, ref), UNPROVABLE)

    def test_a_section_without_any_secdef_refuses_before_any_mutator_for_either_reference(self) -> None:
        for ref in ('1', '2'):
            self.assert_refused_untouched(bound_doc(ONE_NO_SECDEF, ref), UNPROVABLE)

    def test_public_bundle_caller_reports_a_clean_refusal_not_a_mutation_error(self) -> None:
        for layout in (ONE_MISSING_SHAPE, ONE_NO_SECDEF):
            hwp = bound_doc(layout, '2')
            before = state(hwp)
            with self.assertRaises(LocalCliRuntimeError) as ctx:
                _Service()._bundle_official_numbering_apply(hwp, dict(NUMBERING_STEP), protection=census_module.live_protection_reader(hwp))
            self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
            self.assertEqual(mutating_calls(hwp), [])
            self.assertEqual(state(hwp), before)

    def test_one_unbound_section_refuses_even_when_the_number_paragraph_names_another_definition(self) -> None:
        for layout in (S0_BOUND_S1_NO_SECDEF, S0_BOUND_S1_NO_SHAPE):
            self.assert_refused_untouched(bound_doc(layout, '2', numbers=(2, 3)), UNPROVABLE)

    def test_known_shared_reference_keeps_its_specific_refusal_when_another_section_is_unbound(self) -> None:
        for layout in (S0_BOUND_S1_NO_SECDEF, S0_BOUND_S1_NO_SHAPE):
            self.assert_refused_untouched(bound_doc(layout, '1', numbers=(2, 3)), SHARED)

    def test_every_section_bound_and_separate_references_still_proceed(self) -> None:
        hwp = bound_doc(BOTH_BOUND, '2', numbers=(2, 3))
        result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(executes(hwp), ['OutlineNumber', 'ParagraphShape'])
        self.assertTrue(result['changed'])
        self.assertEqual(headings(hwp), [0, 1, 2, 2])

    def test_explicit_shared_reference_is_refused_and_explicit_separate_reference_proceeds(self) -> None:
        self.assert_refused_untouched(bound_doc(ONE_BOUND, '1'), SHARED)
        hwp = bound_doc(ONE_BOUND, '2')
        result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(executes(hwp), ['OutlineNumber', 'ParagraphShape'])
        self.assertTrue(result['changed'])
        self.assertEqual(headings(hwp), [0, 1, 2, 0])


class UnrelatedPathsStayUsableTests(unittest.TestCase):
    LAYOUTS = (ONE_MISSING_SHAPE, ONE_NO_SECDEF, S0_BOUND_S1_NO_SECDEF)

    def test_document_without_a_number_paragraph_is_not_blocked_by_missing_bindings(self) -> None:
        for layout in self.LAYOUTS:
            hwp = bound_doc(layout, '2', numbers=())
            result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
            self.assertEqual(executes(hwp), ['OutlineNumber', 'ParagraphShape'], msg=layout)
            self.assertTrue(result['changed'])
            self.assertEqual(headings(hwp), [0, 1, 0, 0])

    def test_official_definition_numbers_without_redefining_beside_unknown_sharing(self) -> None:
        for layout in self.LAYOUTS:
            hwp = bound_doc(layout, '2', foreign=False)
            result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
            self.assertEqual(executes(hwp), ['ParagraphShape'], msg=layout)
            self.assertTrue(result['changed'])
            self.assertEqual(headings(hwp)[1], 1)

    def test_page_number_insertion_is_unaffected_by_missing_bindings(self) -> None:
        for layout in self.LAYOUTS:
            hwp = bound_doc(layout, '2')
            result = nn.apply_page_number(hwp, POSITION, protection=census_module.live_protection_reader(hwp))
            self.assertEqual(result['status'], 'inserted', msg=layout)


if __name__ == '__main__':
    unittest.main()
