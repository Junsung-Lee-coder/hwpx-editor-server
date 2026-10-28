"""Outline redefinition must be refused before the first mutator unless nothing else can share the definition.

OutlineNumber maps to SecDef.OutlineShape, so redefining it can change an unrelated NUMBER paragraph that uses the
same numbering definition and an OUTLINE paragraph of another section that uses the same global definition. A NUMBER
paragraph whose sharing is unknown (shares_outline_numbering is None) is not proof of separation, and no census
field binds another section's outline paragraph to its own definition, so both are refused while a definition would
change. Paths that do not change the definition stay usable.

The HWPML2X samples and the HeadingIdRef/SECDEF serialization are SYNTHETIC; these tests prove the code flow of this
source against fakes only and make no claim about installed Hancom output, rendering or multi-section behaviour.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import unittest
from typing import Any

from app import native_numbering as nn
from app import native_numbering_census as census_module
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from tests.native_numbering_fakes import FakeCtrl, shape_items
from tests.test_native_numbering_census import _HwpmlHwp
from tests.test_native_numbering_repair import NUMBERING_STEP, POSITION, _Service
from tests.test_native_numbering_review_contract import FOREIGN, TAIL, doc, executes, headings, run_official
from tests.test_native_numbering_shared_outline import _SharedHwp

REFUSAL_CODE = 'outline_usage_unprovable'


def split_sections(base: type, secdef_ref: str = '1') -> type:
    """Move the last paragraph into a second SECTION whose own SECDEF carries OutlineShape=secdef_ref."""

    class SplitSections(base):  # type: ignore[misc, valid-type]
        def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
            text = super().get_text_file(fmt, option)
            if fmt != 'HWPML2X':
                return text
            root = ET.fromstring(text)
            body = root.find('BODY')
            section = body.find('SECTION')
            paragraph = list(section)[-1]
            section.remove(paragraph)
            second = ET.SubElement(body, 'SECTION', {'Id': '1'})
            second.append(paragraph)
            paragraph.find('TEXT').insert(0, ET.Element('SECDEF', {'OutlineShape': secdef_ref}))
            root.find('HEAD').set('SecCnt', '2')
            return ET.tostring(root, encoding='unicode')

    return SplitSections


def scoped_doc(cls: type = _HwpmlHwp, *, heading: int = 2, foreign: bool = True, number_ref: str | None = None) -> Any:
    hwp = doc(foreign=foreign, texts=('Title', 'Item text', 'Numbered'), cls=cls, controls=[FakeCtrl('secd')])
    hwp.paragraphs[2]['heading_type'] = heading
    hwp.hwpml_extras = {'extra_numberings': ('(^9)', '(^8)')}
    if number_ref is not None:
        hwp.number_ref = number_ref
    return hwp


def mutating_calls(hwp: Any) -> list[tuple]:
    return [call for call in hwp.calls if call[0] in {'Execute', 'Run'}]


def state(hwp: Any) -> dict[str, Any]:
    census = census_module.live_protection_reader(hwp)()
    return {
        'numberings_sha256': census['numberings_sha256'],
        'outline_shape': dict(hwp.outline_shape),
        'paragraphs': [dict(p) for p in hwp.paragraphs],
        'headings': headings(hwp),
    }


class UnknownSharingNumberParagraphTests(unittest.TestCase):
    def assert_refused_untouched(self, hwp: Any, level: int = 2) -> nn.NativeNumberingRefusal:
        before = state(hwp)
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, level, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual(executes(hwp), [])
        self.assertEqual(state(hwp), before)
        return ctx.exception

    def test_number_paragraph_without_a_numbering_reference_refuses_before_any_mutator(self) -> None:
        hwp = scoped_doc()
        self.assertEqual(
            [p.get('shares_outline_numbering') for p in census_module.live_protection_reader(hwp)()['paragraphs']],
            [False, False, None],
        )
        self.assertEqual(self.assert_refused_untouched(hwp).code, REFUSAL_CODE)

    def test_every_supported_level_refuses_on_unknown_sharing(self) -> None:
        for level in (1, 2, 3, 4):
            self.assertEqual(self.assert_refused_untouched(scoped_doc(), level).code, REFUSAL_CODE, msg=level)

    def test_unknown_sharing_in_another_section_refuses_before_any_mutator(self) -> None:
        self.assertEqual(self.assert_refused_untouched(scoped_doc(split_sections(_HwpmlHwp))).code, REFUSAL_CODE)

    def test_public_bundle_caller_reports_a_clean_refusal_not_a_mutation_error(self) -> None:
        hwp = scoped_doc()
        before = state(hwp)
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            _Service()._bundle_official_numbering_apply(hwp, dict(NUMBERING_STEP), protection=census_module.live_protection_reader(hwp))
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual(state(hwp), before)

    def test_known_shared_reference_is_still_refused_with_the_foreign_definition_code(self) -> None:
        hwp = scoped_doc(_SharedHwp, number_ref='1')
        self.assertEqual(self.assert_refused_untouched(hwp).code, 'foreign_outline_definition')

    def test_known_separate_reference_still_proceeds(self) -> None:
        hwp = scoped_doc(_SharedHwp, number_ref='2')
        result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(executes(hwp), ['OutlineNumber', 'ParagraphShape'])
        self.assertTrue(result['changed'])
        self.assertEqual(headings(hwp), [0, 1, 2])
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item text', 'Numbered'])


class OtherSectionOutlineParagraphTests(unittest.TestCase):
    def test_outline_paragraph_of_another_section_refuses_before_any_mutator(self) -> None:
        hwp = scoped_doc(split_sections(_HwpmlHwp), heading=1)
        before = state(hwp)
        census = census_module.live_protection_reader(hwp)()
        self.assertEqual([(p['heading'], p['section']) for p in census['paragraphs']], [('NONE', 0), ('NONE', 0), ('OUTLINE', 1)])
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(ctx.exception.code, REFUSAL_CODE)
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual(executes(hwp), [])
        self.assertEqual(state(hwp), before)

    def test_public_bundle_caller_reports_a_clean_refusal_for_the_other_section(self) -> None:
        hwp = scoped_doc(split_sections(_HwpmlHwp), heading=1)
        before = state(hwp)
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            _Service()._bundle_official_numbering_apply(hwp, dict(NUMBERING_STEP), protection=census_module.live_protection_reader(hwp))
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual(state(hwp), before)

    def test_other_section_outline_paragraph_is_refused_even_when_its_section_names_another_definition(self) -> None:
        hwp = scoped_doc(split_sections(_HwpmlHwp, '3'), heading=1)
        before = state(hwp)
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(ctx.exception.code, REFUSAL_CODE)
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual(state(hwp), before)

    def test_other_section_number_paragraph_with_a_known_separate_reference_is_not_blocked(self) -> None:
        hwp = scoped_doc(split_sections(_SharedHwp, '3'), number_ref='2')
        census = census_module.live_protection_reader(hwp)()
        self.assertEqual([(p['heading'], p['section'], p['shares_outline_numbering']) for p in census['paragraphs']][2], ('NUMBER', 1, False))
        result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(executes(hwp), ['OutlineNumber', 'ParagraphShape'])
        self.assertTrue(result['changed'])


    def test_second_section_referencing_the_same_definition_refuses_before_any_mutator(self) -> None:
        hwp = scoped_doc(split_sections(_SharedHwp, '1'), number_ref='2')
        before = state(hwp)
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(ctx.exception.code, REFUSAL_CODE)
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual(state(hwp), before)


    def test_other_section_definition_identical_to_the_target_definition_refuses_before_any_mutator(self) -> None:
        hwp = doc(foreign=True)
        hwp.other_section_shape = {**shape_items(FOREIGN), **TAIL}
        hwp.paragraphs.append({'text': 'Next section', 'heading_type': 0, 'level': 0, 'section': 1})
        before = (dict(hwp.outline_shape), dict(hwp.other_section_shape))
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, 2)
        self.assertEqual(ctx.exception.code, REFUSAL_CODE)
        self.assertEqual(mutating_calls(hwp), [])
        self.assertEqual((hwp.outline_shape, hwp.other_section_shape), before)


class NoDefinitionChangeStaysUsableTests(unittest.TestCase):
    def test_official_definition_numbers_with_unknown_sharing_and_an_other_section_outline(self) -> None:
        for cls, heading in ((_HwpmlHwp, 2), (split_sections(_HwpmlHwp), 2), (split_sections(_HwpmlHwp), 1)):
            hwp = scoped_doc(cls, heading=heading, foreign=False)
            result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
            self.assertEqual(executes(hwp), ['ParagraphShape'], msg=(cls.__name__, heading))
            self.assertTrue(result['changed'])
            self.assertEqual(headings(hwp)[1], 1)

    def test_already_official_target_is_reported_unchanged_without_any_mutator(self) -> None:
        hwp = scoped_doc(split_sections(_HwpmlHwp), heading=1, foreign=False)
        hwp.paragraphs[1].update(heading_type=1, level=1)
        result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(result['status'], 'already_applied')
        self.assertFalse(result['changed'])
        self.assertEqual(mutating_calls(hwp), [])


class PageNumberUnaffectedTests(unittest.TestCase):
    def test_page_number_is_inserted_beside_unknown_sharing_and_other_section_outline_paragraphs(self) -> None:
        for cls, heading in ((_HwpmlHwp, 2), (split_sections(_HwpmlHwp), 1)):
            hwp = scoped_doc(cls, heading=heading)
            result = nn.apply_page_number(hwp, POSITION, protection=census_module.live_protection_reader(hwp))
            self.assertEqual(result['status'], 'inserted', msg=(cls.__name__, heading))


if __name__ == '__main__':
    unittest.main()
