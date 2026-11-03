"""A section break is qualified only with a text character immediately on both sides of the caret (M6).

Text elsewhere in the paragraph is not enough: a caret directly before or after a mid-paragraph inline control (here
a bookmark between "ab" and "cd") is refused before BreakSection with section_insert_position_unsupported, under
both accepted readings of an inline control's caret width (8 positions, or none). Between two characters the break
still runs. Through the real public bundle and owning worker over the synthetic layout SDK.
"""
from __future__ import annotations

import unittest

from tests.test_bundle_step_local_refusal import owner_for
from tests.test_deferred_step_intent import _Checks, counted, modifies, run
from tests.test_layout_exact import _FakeLayoutHwp

CODE = 'section_insert_position_unsupported'
MARK = '|'


class _ControlInText(_FakeLayoutHwp):
    """The fake layout SDK whose paragraph text may hold MARK, read back as an inline BOOKMARK control there."""

    def GetTextFile(self, fmt, option):  # noqa: N802
        return super().GetTextFile(fmt, option).replace(MARK, '</CHAR><BOOKMARK Name="m"/><CHAR>')


def doc(caret):
    hwp = counted(_ControlInText([['hello world', 'ab' + MARK + 'cd', 'tail']]))
    for name in ('execute', 'run'):
        modifies(hwp, name, lambda action, *rest: action in _FakeLayoutHwp.MUTATING and action not in hwp.noop)
    hwp.fault_mark = None
    hwp.caret = tuple(caret)
    return hwp


def insert(pos):
    return {'op': 'layout_exact', 'kind': 'section_insert', 'confirm_layout': True, 'expected_pos': list(pos)}


class InlineControlNeighbours(_Checks):
    def test_a_caret_next_to_an_inline_control_is_refused_before_break_section(self):
        cases = {
            'directly after the control (8-position reading)': (0, 1, 10),
            'next to the control (no-width reading, either side)': (0, 1, 2),
        }
        for label, caret in cases.items():
            with self.subTest(label):
                hwp = doc(caret)
                owner = owner_for(self, hwp)
                result = run(owner, insert(caret))
                self.assert_clean_refusal(owner, result, 0, CODE, dirty=False)
                self.assertEqual(hwp.mutations(), [])
                self.assertEqual(len(hwp.sections), 1)
                hwp.caret = (0, 0, 6)
                self.assert_continues_and_saves(owner, insert((0, 0, 6)))
                self.assertEqual(len(hwp.sections), 2)

    def test_between_two_characters_beside_the_control_still_breaks(self):
        for caret in ((0, 1, 1), (0, 1, 11), (0, 1, 3)):
            with self.subTest(caret=caret):
                hwp = doc(caret)
                owner = owner_for(self, hwp)
                run(owner, insert(caret))
                self.assertEqual(hwp.mutations(), ['BreakSection'])


if __name__ == '__main__':
    unittest.main()
