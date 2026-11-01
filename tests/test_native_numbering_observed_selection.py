"""Regression for the observed native no-selection shape (BOOK, pyhwpx 1.7.2, Hancom 2024).

The first real MCP numbering request stopped before any target lookup because the locked wrapper's
``get_selected_pos`` returns ``(False, None, None, None, None, None, None)`` without a block, and the parser
accepted only integer coordinates. These are portable source tests; the native run is recorded separately.
"""
from __future__ import annotations

import unittest

from app import native_numbering as nn


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


if __name__ == '__main__':
    unittest.main()
