"""Regressions for behavior observed on real Hancom (BOOK, pyhwpx 1.7.2, Hancom 2024) through MCP.

Each case replays a shape the locked wrapper actually returned and that stopped a real numbering request.
These are portable source tests; the native runs are recorded separately.
"""
from __future__ import annotations

import unittest

from app import native_numbering as nn
from app.local_cli_bundle_controls import LocalCliBundleControlsMixin
from app.local_cli_runtime import LocalCliRuntimeError


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


if __name__ == '__main__':
    unittest.main()
