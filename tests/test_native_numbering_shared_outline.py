"""A NUMBER paragraph that points at the numbering definition used as a section outline must stop the outline redefinition.

Redefining the section OutlineShape rewrites that shared numbering definition, so the NUMBER paragraph's marker would
change too. The refusal must come before OutlineNumber is issued, not as an unknown outcome afterwards.

The HWPML2X samples are SYNTHETIC (element and attribute names are not verified against Hancom output); these tests
prove the reader and refusal logic of this source only and make no native claim.
"""
from __future__ import annotations

import unittest
from typing import Any

from app import native_numbering as nn
from app import native_numbering_census as census_module
from tests.native_numbering_fakes import FakeCtrl
from tests.test_native_numbering_census import _HwpmlHwp, hwpml
from tests.test_native_numbering_review_contract import doc, executes, headings, run_official

PARAS = [('Title', 0, 0), ('Item text', 0, 0), ('Numbered', 2, 0)]


def _with_number_ref(text: str, ref: str) -> str:
    return text.replace('HeadingType="Number"', f'HeadingType="Number" HeadingIdRef="{ref}"')


class _SharedHwp(_HwpmlHwp):
    number_ref = '1'

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
        text = super().get_text_file(fmt, option)
        return _with_number_ref(text, self.number_ref) if fmt == 'HWPML2X' else text


def shared_doc(number_ref: str) -> _SharedHwp:
    hwp = doc(foreign=True, texts=('Title', 'Item text', 'Numbered'), cls=_SharedHwp, controls=[FakeCtrl('secd')])
    hwp.paragraphs[2]['heading_type'] = 2
    hwp.number_ref = number_ref
    hwp.hwpml_extras = {'extra_numberings': ('(^9)',)}
    return hwp


class CensusSharedOutlineTests(unittest.TestCase):
    def flags(self, ref: str) -> list[Any]:
        parsed = census_module.parse_hwpml2x_census(_with_number_ref(hwpml(PARAS, extra_numberings=('(^9)',)), ref))
        return [item.get('shares_outline_numbering') for item in parsed['paragraphs']]

    def test_number_paragraph_using_the_outline_numbering_definition_is_flagged(self) -> None:
        self.assertEqual(self.flags('1'), [False, False, True])

    def test_number_paragraph_using_another_numbering_definition_is_not_flagged(self) -> None:
        self.assertEqual(self.flags('2'), [False, False, False])

    def test_number_paragraph_without_a_numbering_reference_is_left_unclaimed(self) -> None:
        parsed = census_module.parse_hwpml2x_census(hwpml(PARAS))
        self.assertEqual([item.get('shares_outline_numbering') for item in parsed['paragraphs']], [False, False, None])

    def test_validator_accepts_only_boolean_or_missing_flags(self) -> None:
        parsed = census_module.parse_hwpml2x_census(_with_number_ref(hwpml(PARAS), '1'))
        self.assertTrue(nn._validated_census(parsed)['paragraphs'][2]['shares_outline_numbering'])  # noqa: SLF001
        for bad in ('yes', 1, 0):
            parsed['paragraphs'][2]['shares_outline_numbering'] = bad
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                nn._validated_census(parsed)  # noqa: SLF001


class SharedOutlineRefusalTests(unittest.TestCase):
    def test_shared_numbering_definition_is_refused_before_any_native_call(self) -> None:
        hwp = shared_doc('1')
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(ctx.exception.code, 'foreign_outline_definition')
        self.assertEqual(executes(hwp), [])
        self.assertEqual(headings(hwp), [0, 0, 2])

    def test_a_separate_numbering_definition_still_proceeds(self) -> None:
        hwp = shared_doc('2')
        result = run_official(hwp, 2, protection=census_module.live_protection_reader(hwp))
        self.assertEqual(executes(hwp), ['OutlineNumber', 'ParagraphShape'])
        self.assertTrue(result['changed'])
        self.assertEqual(headings(hwp), [0, 1, 2])
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item text', 'Numbered'])


if __name__ == '__main__':
    unittest.main()
