"""Section-break caret bounds (N3), contradictory strict section counts (N5) and truthful refusal text (N6).

Through the real public bundle and owning worker over the synthetic layout SDK.

- N3: section_insert is qualified only between two characters of one paragraph's text. At a paragraph's first text
  (position 0, or after its leading inline controls), at its end, or in a paragraph without text it is refused before
  BreakSection with section_insert_position_unsupported; the session stays usable and the next edit and the save work.
- N5: KeyIndicator and the HWPML readback disagreeing on the section count is an unknown editor state: held, no
  refusal_code, no DeleteBack and no further SDK call after the readback that exposed it.
- N6: a clean refusal puts the caret back, which can clear an existing selection; the refusal text and the MCP note
  say so.
"""
from __future__ import annotations

import json
import unittest

from tests.test_deferred_step_intent import _Checks, layout_doc, run
from tests.test_bundle_step_local_refusal import owner_for
from tests.test_guarded_bundle_observations import real_owned
from tests.test_layout_exact import _page_step

INSERT = {'op': 'layout_exact', 'kind': 'section_insert', 'confirm_layout': True}
CODE = 'section_insert_position_unsupported'


def insert(pos):
    return dict(INSERT, expected_pos=list(pos))


class SectionInsertPosition(_Checks):
    """Paragraph 0 of the fixture opens the section with SECDEF and COLDEF (8 positions each) before its text."""

    def doc(self, caret):
        hwp = layout_doc(sections=[['hello world', 'next']])
        hwp.caret = tuple(caret)
        return hwp

    def test_non_interior_positions_are_refused_before_break_section(self):
        cases = {
            'paragraph start': (0, 1, 0),
            'section start before its definitions': (0, 0, 0),
            'first text after the leading definitions': (0, 0, 16),
            'paragraph end': (0, 1, 4),
            'end of the first paragraph': (0, 0, 11),
        }
        for label, caret in cases.items():
            with self.subTest(label):
                hwp = self.doc(caret)
                owner = owner_for(self, hwp)
                result = run(owner, insert(caret))
                self.assert_clean_refusal(owner, result, 0, CODE, dirty=False)
                self.assertEqual(hwp.mutations(), [])
                self.assertEqual(len(hwp.sections), 1)
                self.assertEqual(hwp.caret, caret)
                # The same session goes on: an interior break, then the save.
                hwp.caret = (0, 0, 6)
                self.assert_continues_and_saves(owner, insert((0, 0, 6)))
                self.assertEqual(len(hwp.sections), 2)

    def test_interior_position_proceeds(self):
        for caret in ((0, 0, 6), (0, 1, 2)):
            with self.subTest(caret=caret):
                hwp = self.doc(caret)
                owner = owner_for(self, hwp)
                result = run(owner, insert(caret))
                self.assertTrue(result['ok'], result)
                self.assertEqual(hwp.mutations(), ['BreakSection'])

    def test_a_paragraph_without_text_is_refused(self):
        hwp = layout_doc(sections=[['', 'next']])
        hwp.caret = (0, 0, 0)
        owner = owner_for(self, hwp)
        result = run(owner, insert((0, 0, 0)))
        self.assert_clean_refusal(owner, result, 0, CODE, dirty=False)
        self.assertEqual(hwp.mutations(), [])


class ContradictorySectionCount(_Checks):
    def test_keyindicator_and_hwpml_disagreeing_is_held(self):
        hwp = layout_doc(sections=[['one', 'two'], ['three']])
        hwp.caret = (0, 2, 0)
        plain_key = hwp.KeyIndicator
        hwp.KeyIndicator = lambda: (True, 3, *plain_key()[2:])  # three sections, HWPML has two
        plain_text = hwp.GetTextFile
        marks = []

        def get_text(*args):
            value = plain_text(*args)
            marks.append(len(hwp.sdk_calls))
            return value
        hwp.GetTextFile = get_text
        owner = owner_for(self, hwp)
        step = {'op': 'layout_exact', 'kind': 'section_delete', 'expected_pos': [0, 2, 0], 'section_index': 2, 'confirm_layout': True}
        result = run(owner, step)
        self.assertEqual(hwp.mutations(), [])
        self.assertTrue(marks)
        self.assertEqual(len(hwp.sdk_calls), marks[0], hwp.sdk_calls[marks[0]:])
        self.assert_held(owner, hwp, result, mutated=False)


class RefusalTextIsTruthful(unittest.TestCase):
    def test_refusal_text_says_a_selection_may_be_cleared(self):
        hwp = layout_doc()
        hwp.selection = (0, 0, 0, 1)
        owner = real_owned(self, hwp)
        result = run(owner, _page_step())
        step = result['steps'][-1]
        self.assertEqual(step.get('refusal_code'), 'editor_not_normal', step)
        self.assertIn('the document is unchanged and the caret is put back; an existing selection may be cleared', step['error'])
        self.assertNotIn('document and caret are unchanged', step['error'])
        self.assertIsNone(hwp.selection)

    def test_mcp_notes_say_a_selection_may_be_cleared(self):
        from hwpx_mcp.operations import LayoutStep, ObjectInsertStep, ParagraphRehomeStep
        from hwpx_mcp import parity
        for model in (LayoutStep, ObjectInsertStep, ParagraphRehomeStep):
            text = json.dumps(model.model_json_schema(), ensure_ascii=False)
            self.assertIn('an existing selection may be cleared', text, model.__name__)
            self.assertNotIn('document and caret are unchanged', text, model.__name__)
        hold = parity.BUNDLE_OPS['layout_exact']['error_hold']
        self.assertIn('existing selection may be cleared', hold)


if __name__ == '__main__':
    unittest.main()
