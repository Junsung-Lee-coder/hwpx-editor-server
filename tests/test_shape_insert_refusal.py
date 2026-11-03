"""Drawn shapes outside the characterized drawing contract never report success (S1).

A diagonal line (both width_mm and height_mm non-zero) has no characterized drawing contract: object_insert_exact
refuses it from the request alone with the fixed code unverifiable_layout. No readback, no native action, no edit;
the session stays usable and saves, and a horizontal line in the same session is drawn.

An SDK whose shape parameter set has only the frame items (no typed drawing subsets) is the state in which Hancom
saved invisible shapes: the engine cannot prepare the drawing, so it never executes, and the session is held as a
technical failure, not refused and not reported as drawn.

Through the real public bundle and owning worker over the synthetic object SDKs.
"""
from __future__ import annotations

import unittest

from tests.test_bundle_step_local_refusal import owner_for
from tests.test_deferred_step_intent import _Checks, object_doc, object_step, run
from tests.test_shape_drawing import drawing_doc, shape_step


def diagonal(hwp, inline):
    return object_step(hwp, kind='line', text=None, treat_as_char=inline, width_mm=40.0, height_mm=10.0)


class ShapesOutsideTheDrawingContract(_Checks):
    def test_a_diagonal_line_is_refused_before_any_sdk_call_and_the_session_continues(self):
        for prior_dirty in (False, True):
            for inline in (True, False):
                with self.subTest(prior_dirty=prior_dirty, inline=inline):
                    hwp = drawing_doc()
                    owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                    xml = hwp.xml()
                    hwp.sdk_calls.clear()
                    result = run(owner, diagonal(hwp, inline))
                    self.assert_clean_refusal(owner, result, 0, 'unverifiable_layout', dirty=prior_dirty)
                    self.assertNotIn('native', hwp.sdk_calls)
                    self.assertNotIn('GetTextFile', hwp.sdk_calls)
                    self.assertEqual(hwp.log, [])
                    self.assertEqual(hwp.xml(), xml)
                    self.assert_continues_and_saves(owner, shape_step(hwp, 'line_h', inline))
                    self.assertIn('<LINE ', hwp.xml())

    def test_a_frame_only_shape_sdk_never_executes(self):
        for kind, size in (('line', {'width_mm': 40.0, 'height_mm': 0.0}), ('rectangle', {'width_mm': 30.0, 'height_mm': 12.0}),
                           ('ellipse', {'width_mm': 40.0, 'height_mm': 20.0})):
            for inline in (True, False):
                with self.subTest(kind=kind, inline=inline):
                    hwp = object_doc()
                    owner = owner_for(self, hwp)
                    xml = hwp.xml()
                    result = run(owner, object_step(hwp, kind=kind, text=None, treat_as_char=inline, **size))
                    self.assertFalse(any(entry.startswith('Execute') for entry in hwp.log), hwp.log)
                    self.assertEqual(hwp.xml(), xml)
                    self.assertNotIn('SHAPEOBJECT', hwp.xml())
                    self.assert_held(owner, hwp, result, mutated=False)


if __name__ == '__main__':
    unittest.main()
