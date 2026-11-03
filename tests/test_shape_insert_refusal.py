"""Drawn shapes without a qualified drawing contract are refused before any SDK call (S1, first safety stage).

On Hancom 2024 a line/rectangle/ellipse inserted with only the frame size (Width, Height, TreatAsChar) is saved
with no outline and a zero or degenerate drawn geometry, so nothing renders, while the old verifier reported
success. Until a kind has a qualified drawing contract, object_insert_exact refuses it from the request alone with
the fixed code unverifiable_layout: no readback, no native action, no edit; the session stays usable and saves.
Through the real public bundle and owning worker over the synthetic object SDK.
"""
from __future__ import annotations

import unittest

from tests.test_bundle_step_local_refusal import owner_for
from tests.test_deferred_step_intent import _Checks, object_doc, object_step, run

SHAPES = {
    'line': {'width_mm': 40.0, 'height_mm': 0.0},
    'rectangle': {'width_mm': 30.0, 'height_mm': 12.0},
    'ellipse': {'width_mm': 40.0, 'height_mm': 20.0},
}


def shape_step(hwp, kind, treat_as_char):
    return object_step(hwp, kind=kind, text=None, treat_as_char=treat_as_char, **SHAPES[kind])


class UnqualifiedShapesAreRefused(_Checks):
    def test_each_shape_kind_is_refused_before_any_sdk_call_and_the_session_continues(self):
        for prior_dirty in (False, True):
            for kind in SHAPES:
                for treat_as_char in (True, False):
                    with self.subTest(prior_dirty=prior_dirty, kind=kind, treat_as_char=treat_as_char):
                        hwp = object_doc()
                        owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                        xml = hwp.xml()
                        hwp.sdk_calls.clear()
                        result = run(owner, shape_step(hwp, kind, treat_as_char))
                        self.assert_clean_refusal(owner, result, 0, 'unverifiable_layout', dirty=prior_dirty)
                        self.assertNotIn('native', hwp.sdk_calls)
                        self.assertNotIn('GetTextFile', hwp.sdk_calls)
                        self.assertEqual(hwp.xml(), xml)
                        self.assert_continues_and_saves(owner, object_step(hwp))
                        self.assertIn('<FOOTNOTE>', hwp.xml())
                        self.assertNotIn('SHAPEOBJECT', hwp.xml())


if __name__ == '__main__':
    unittest.main()
