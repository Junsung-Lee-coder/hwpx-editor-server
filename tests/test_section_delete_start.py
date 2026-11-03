"""section_delete from the caret Hancom gives at a section's start.

On Hancom 2024 the first text of a new section's first paragraph is at position 16, after its inline SECDEF and
COLDEF (8 positions each), and MoveLeft from there leaves the paragraph, so position 0 cannot be reached by
navigation (and a caret set to 0 is not kept there). section_delete therefore accepts expected_pos at 0, 8 or 16 and,
after proving the section start, runs the one DeleteBack at that position; the readback decides the outcome.
"""
from __future__ import annotations

import unittest

from app.local_cli_runtime import LocalCliRuntimeError
from tests.test_layout_exact import _FakeLayoutHwp, _run


class HancomSectionStart(_FakeLayoutHwp):
    """MoveLeft at a section's first text position (16, after the two inline definitions) leaves the paragraph."""

    def run(self, name):
        if name == 'DeleteBack' and self.caret[2] == 16:
            self.caret = (0, self.caret[1], 0)  # modelled: Backspace at the first text removes the section break
        if name == 'MoveLeft' and self.caret[2] == 16:
            self.log.append(name)
            para = self.caret[1]
            self.caret = (0, para - 1, len(self._para(para - 1)['text']))
            return True
        return super().run(name)


def step(pos, index=2, para=2):
    return {'kind': 'section_delete', 'expected_pos': [0, para, pos], 'confirm_layout': True, 'section_index': index}


class SectionDeleteFromTextStart(unittest.TestCase):
    def test_delete_from_the_first_text_position(self):
        hwp = HancomSectionStart([['a', 'b'], ['c', 'd'], ['e']])
        hwp.caret = (0, 2, 16)
        result = _run(hwp, step(16))
        self.assertEqual(len(hwp.sections), 2)
        self.assertEqual(hwp.mutations(), ['DeleteBack'])

    def test_other_positions_are_refused_before_any_edit(self):
        hwp = HancomSectionStart([['a', 'b'], ['c', 'd'], ['e']])
        hwp.caret = (0, 2, 3)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'start of the section'):
            _run(hwp, step(3))
        self.assertEqual(hwp.mutations(), [])


if __name__ == '__main__':
    unittest.main()
