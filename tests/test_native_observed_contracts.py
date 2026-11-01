"""Regressions for behavior observed on real Hancom (BOOK, pyhwpx 1.7.2, Hancom 2024) through MCP.

Each case replays a shape the locked wrapper actually returned and that stopped a real numbering request.
These are portable source tests; the native runs are recorded separately.
"""
from __future__ import annotations

import unittest

from app import native_numbering as nn
from app.local_cli_bundle_controls import LocalCliBundleControlsMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.native_numbering_census import live_protection_reader
from tests.native_numbering_fakes import FakeCtrl, shape_items
from tests.test_native_numbering_census import _HwpmlHwp


class ObservedNoSelection(unittest.TestCase):
    def test_exact_native_no_block_value_is_no_selection(self) -> None:
        self.assertEqual(nn._parse_selected_pos((False, None, None, None, None, None, None)), ())
        self.assertEqual(nn._parse_selected_pos([False, None, None, None, None, None, None]), ())

    def test_target_state_reads_the_native_no_block_value(self) -> None:
        class Hwp:
            def get_pos(self):
                return (0, 3, 0)

            def get_selected_pos(self):
                return (False, None, None, None, None, None, None)

        self.assertEqual(nn._read_target_state(Hwp()), ((0, 3, 0), ()))

    def test_integer_forms_keep_their_meaning(self) -> None:
        self.assertEqual(nn._parse_selected_pos((False, 0, 0, 0, 0, 0, 0)), ())
        self.assertEqual(nn._parse_selected_pos((True, 0, 1, 2, 0, 1, 9)), (0, 1, 2, 0, 1, 9))

    def test_a_block_or_partial_shape_without_coordinates_stays_noncanonical(self) -> None:
        for value in (
            (True, None, None, None, None, None, None),
            (False, 0, 0, 0, 0, 0, None),
            (False, None, None, None, None, None, 0),
            (False, None, None, None, None, None),
            (None, None, None, None, None, None, None),
            (0, None, None, None, None, None, None),
        ):
            with self.subTest(value=value):
                self.assertIsNone(nn._parse_selected_pos(value))


class ObservedPageProperty(unittest.TestCase):
    """pyhwpx 1.7.2 declares ``current_page`` as an int property, not a method."""

    def evidence(self, hwp):
        return LocalCliBundleControlsMixin._bundle_page_evidence(LocalCliBundleControlsMixin(), hwp, numbering_guard=True)

    def test_exact_int_property_is_the_page(self) -> None:
        class Hwp:
            @property
            def current_page(self):
                return 2

        self.assertEqual(self.evidence(Hwp()), {'page': 2, 'method': 'current_page'})

    def test_method_spelling_still_works(self) -> None:
        class Hwp:
            def current_page(self):
                return 3

        self.assertEqual(self.evidence(Hwp()), {'page': 3, 'method': 'current_page'})

    def test_noninteger_or_failing_property_is_not_a_page(self) -> None:
        for value in (0, -1, True, '2', 2.0, None):
            class Hwp:
                current_page = value

            with self.subTest(value=value), self.assertRaises(LocalCliRuntimeError):
                self.evidence(Hwp())

        class Raising:
            @property
            def current_page(self):
                raise RuntimeError('native failure')

        with self.assertRaises(RuntimeError):
            self.evidence(Raising())


class ObservedOutlineDefinitionAssignsTheParagraph(unittest.TestCase):
    """Hancom 2024: OutlineNumber on a plain caret paragraph also gives it HeadingType=1/Level=0."""

    def doc(self, assigned=(1, 0)):
        hwp = _HwpmlHwp(
            paragraphs=[{'text': 'Title', 'heading_type': 0, 'level': 0}, {'text': 'Item', 'heading_type': 0, 'level': 0}],
            controls=[FakeCtrl('secd')],
        )
        hwp.outline_shape.update(shape_items((('^1.', 0), ('^2:', 8), ('^3)', 0), ('^4)', 8))))
        execute = hwp.HAction.Execute

        def native_execute(name, hset):
            result = execute(name, hset)
            if name == 'OutlineNumber':
                hwp.current_paragraph().update(heading_type=assigned[0], level=assigned[1])
            return result

        hwp.HAction.Execute = native_execute
        return hwp

    def apply(self, hwp, level):
        return nn.apply_official_numbering_at_match(hwp, find='Item', level=level, protection=live_protection_reader(hwp))

    def test_first_level_is_reached_by_the_definition_alone(self) -> None:
        hwp = self.doc()
        result = self.apply(hwp, 1)
        self.assertEqual(result['status'], 'format_defined')
        self.assertEqual(len(result['native_actions']), 1)
        self.assertEqual((hwp.paragraphs[1]['heading_type'], hwp.paragraphs[1]['level']), (1, 0))

    def test_a_deeper_level_is_one_definition_plus_one_paragraph_write(self) -> None:
        hwp = self.doc()
        result = self.apply(hwp, 3)
        self.assertEqual(len(result['native_actions']), 2)
        self.assertEqual((hwp.paragraphs[1]['heading_type'], hwp.paragraphs[1]['level']), (1, 2))

    def labelled_export(self, hwp):
        plain = hwp.get_text_file

        def get_text_file(fmt='UNICODE', option=''):
            if fmt != 'UNICODE':
                return plain(fmt, option)
            return '\r\n'.join(('1. ' if p['heading_type'] == 1 else '') + str(p['text']) for p in hwp.paragraphs)

        hwp.get_text_file = get_text_file
        return hwp

    def test_the_labelled_text_export_is_admitted_on_the_definition_path_with_full_proof(self) -> None:
        for level in (1, 2):
            with self.subTest(level=level):
                hwp = self.labelled_export(self.doc())
                self.assertTrue(self.apply(hwp, level)['changed'])

    def test_text_loss_on_the_definition_path_stays_unknown(self) -> None:
        hwp = self.labelled_export(self.doc())
        execute = hwp.HAction.Execute

        def losing(name, hset):
            result = execute(name, hset)
            if name == 'OutlineNumber':
                hwp.paragraphs[0]['text'] = 'Tit'
            return result

        hwp.HAction.Execute = losing
        with self.assertRaises(nn.NativeNumberingMutationError):
            self.apply(hwp, 1)

    def test_without_protection_the_text_export_stays_strict(self) -> None:
        hwp = self.labelled_export(self.doc())
        with self.assertRaises((nn.NativeNumberingMutationError, nn.NativeNumberingRefusal)):
            nn.apply_official_numbering_at_match(hwp, find='Item', level=1)

    def test_any_other_paragraph_change_during_the_definition_stays_unknown(self) -> None:
        for assigned in ((1, 2), (2, 0), (3, 0)):
            with self.subTest(assigned=assigned), self.assertRaises(nn.NativeNumberingMutationError):
                self.apply(self.doc(assigned), 1)


if __name__ == '__main__':
    unittest.main()
