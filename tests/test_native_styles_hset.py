"""Named styles: the parameter set's HSet, read before GetDefault takes it.

Apply's HAction.GetDefault receives the set's HSet, which on the pinned wrapper is a COM property of the parameter-set object
(pinned pyhwpx 1.7.2 set_style passes pset.HSet the same way, core.py 2666-2668). Create and change take no HSet: they use the
legacy action family, whose set is passed itself (tests.test_native_styles_legacy). The edit has entered native navigation by then. An HSet read
that raises, returns None or a plain value, or finds no HSet at all, holds the session at once: no GetDefault, no give-back and no
read. A well-typed set whose GetDefault answers False stays the clean refusal style_action_unavailable, with the caret given back
and the document read again to prove it unchanged.

Through the real owned service, with every attribute, call and write on the SDK object recorded (tests.test_native_styles_repairs).
Not Hancom evidence.
"""
from __future__ import annotations

import unittest

from tests import test_native_styles_entry as entry
from tests.test_native_styles import executes
from tests.test_native_styles_repairs import Fault, after_fault, bundle, raising, returning

SETS = entry.SETS
HSET_FAULTS = (('raises', raising(RuntimeError('COM getter failed'))), ('absent', raising(AttributeError('dynamic lookup'))), ('none', returning(None)),
               ('int', returning(1)), ('str', returning('Style')), ('tuple', returning(())))


class PlainSet:
    """A parameter-set object with no HSet at all: no such attribute, descriptor or class member, and no lookup hook."""

    def __init__(self, set_id: str) -> None:
        self.set_id = set_id
        self.items: dict = {}


class ParameterSetHSet(entry._Entry):
    def test_an_hset_that_raises_or_is_not_an_object_holds_before_get_default(self) -> None:
        for op in ('apply',):
            when = f'get:HParameterSet.{SETS[op]}.HSet'
            for name, effect in HSET_FAULTS:
                with self.subTest(op=op, effect=name):
                    owned, inner, trace, outcome = self.faulted(op, Fault(when, effect))
                    self.assert_held(owned, inner, trace, outcome)
                    self.assertNotIn('call:HAction.GetDefault', trace[trace.index('FAULT:' + when):], 'GetDefault is never called')

    def test_after_an_earlier_dirty_edit_an_hset_fault_holds_with_the_debt_kept(self) -> None:
        owned, inner, trace, outcome = self.faulted('apply', Fault('get:HParameterSet.HStyle.HSet', returning(None)), dirty_first=True)
        step = outcome['steps'][-1]
        self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
        self.assertNotIn('refusal_code', step)
        self.assertEqual(after_fault(trace), [])
        self.assertEqual(len(executes(inner)), 1, 'only the earlier edit was written')
        self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')

    def test_a_set_without_any_hset_after_entry_holds_rather_than_refusing(self) -> None:
        """No new healthy path: an HSet absent from a plain object, after the edit entered navigation, is held like a raising
        HSet read, and nothing native follows: the trace is the raising run's trace up to that read."""
        for op in ('apply',):
            with self.subTest(op=op):
                when = f'get:HParameterSet.{SETS[op]}.HSet'
                owned, _inner, raised = self.owned(entry.DOCUMENTS[op])
                request = self.request(owned, op)
                begin = len(raised)
                fault = Fault(when, raising(RuntimeError('COM getter failed')))
                object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                bundle(owned, **request)
                self.assertTrue(fault.fired)
                expected = [item for item in raised[begin:raised.index('FAULT:' + when)] if item != when]

                owned, inner, trace = self.owned(entry.DOCUMENTS[op])
                request = self.request(owned, op)
                setattr(inner.HParameterSet, SETS[op], PlainSet('Style'))
                start = len(trace)
                outcome = bundle(owned, **request)
                step = outcome['steps'][-1]
                self.assertNotIn('refusal_code', step, 'a set without HSet after entry is not a clean refusal')
                self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
                self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(executes(inner), [], 'nothing was written and GetDefault was not called')
                self.assertEqual(trace[start:], expected, 'no native access follows the missing HSet')

    def test_a_well_typed_set_whose_get_default_answers_false_stays_a_clean_refusal(self) -> None:
        for op in ('apply',):
            with self.subTest(op=op):
                owned, inner, trace = self.owned(entry.DOCUMENTS[op])
                request = self.request(owned, op)
                caret = inner.cursor
                inner.defaults_result = False
                start = len(trace)
                step = bundle(owned, **request)['steps'][-1]
                self.assertEqual(step.get('refusal_code'), 'style_action_unavailable', step)
                self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(executes(inner), [])
                self.assertEqual(inner.cursor, caret, 'the caret is given back')
                seen = trace[start:]
                hset = f'get:HParameterSet.{SETS[op]}.HSet'
                self.assertLess(seen.index(hset), seen.index('call:HAction.GetDefault'), 'the HSet was read before GetDefault')
                self.assertIn('call:get_text_file', seen[seen.index('call:HAction.GetDefault'):], 'the document was read again to prove it unchanged')


if __name__ == '__main__':
    unittest.main()
