"""Named styles: a change's entered reads hold at their first malformed answer, before any further SDK call.

Two first-fault rules of a change, once the caret stands at the style's use and the shape has been read:

- **A told item outside its documented representation holds at char_shape.item.** IDHwpParameterSet.Item answers a VARIANT; the
  items a change compares are typed in ParameterSetTable_2504 set 13 (pages 16-18): Height PIT_I4, TextColor PIT_UI4, Bold, Italic,
  UnderlineType and every ratio and relative size PIT_UI1, every spacing and offset PIT_I1, every font name PIT_BSTR. An exact int
  outside its type (UI1 0..255, I1 -128..127, I4 -2^31..2^31-1, and a colour as an unsigned or a signed 32-bit VARIANT,
  -2^31..2^32-1) is a malformed answer, not a value that differs. A typed in-range value that is not the style's own stays the clean
  refusal style_shape_unverified; no range narrower than the type (a usable size, ratio or spacing) is imposed.
- **A change's legacy family is read after entry, so a purely absent member holds.** The change reads the shape before making
  StyleDirectEdit; CreateAction, the action's CreateSet, GetDefault and Execute, and the set's SetItem are read after the caret
  moved. A member absent there holds at its site (create_action.member, action.member, set.member), as every other entered member
  does, and the caret is not given back. Before entry, for apply and create, such an absence stays the clean native_unavailable.
  With a COM dispatch object the pure case cannot arise (its lookup is dynamic); this is consistency with the entered rule.

Through the real owned service, with every attribute, call and write on the SDK object recorded (tests.test_native_styles_repairs):
after the first malformed answer or entered absence, no native call of any kind follows. Not Hancom evidence.
"""
from __future__ import annotations

import json
import math
import unittest

from app import native_styles
from app.native_numbering import NativeNumberingReadFailure, NativeNumberingRefusal
from tests import test_native_styles_entry as entry
from tests.native_styles_fakes import canonical_char, styles_document
from tests.test_native_styles import executes
from tests.test_native_styles_filled import RICH, rich_document, to_11
from tests.test_native_styles_legacy import SECRET, TOUCHED, Hostile, One, TouchyOne, _Legacy, create_request, update_request
from tests.test_native_styles_repairs import Fault, after_fault, bundle, raising, returning
from tests.test_native_styles_snapshot import BOUND

L = entry.LEGACY
ITEM = 'call:CharShape.Item'


class ItemRepresentation(_Legacy):
    """Each told item's Item answer against its documented PIT type, on the rich 바탕글 (tests.test_native_styles_filled)."""

    def answer(self, item: str, value, *, document=rich_document, request_of=to_11, dirty_first: bool = False):
        return self.run_with('update', request_of, Fault(ITEM, returning(value), skip=BOUND.index(item)), document=document, dirty_first=dirty_first)

    def assert_held_at_item(self, item: str, value, returned: str) -> None:
        owned, inner, trace, _start, outcome = self.answer(item, value)
        self.assert_held(owned, inner, trace, outcome)
        diagnostic = self.diagnostic(outcome)
        self.assertEqual((diagnostic.get('native_site'), diagnostic.get('native_return')), ('char_shape.item', returned), outcome)
        self.assertEqual(after_fault(trace), [], 'no caret return, census read, action or write after the malformed answer')
        self.assertEqual(inner.styles[0]['char'], RICH)

    def assert_refused(self, item: str, value) -> None:
        owned, inner, trace, start, outcome = self.answer(item, value)
        step = outcome['steps'][-1]
        self.assertEqual((step.get('refusal_code'), step['mutation_may_have_persisted']), ('style_shape_unverified', False), step)
        self.assertIn(f'its item {item} differs', step.get('refusal_message', ''))
        self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
        after = after_fault(trace)
        self.assertIn('call:set_pos', after, 'a healthy mismatch gives the caret back')
        self.assertIn('call:get_text_file', after, 'and reads the document again')
        self.assertNotIn('call:CreateAction', after)
        self.assertEqual(executes(inner), [])

    def test_a_value_outside_its_pit_representation_holds(self) -> None:
        cases = (('Height', 2 ** 31), ('Height', -2 ** 31 - 1), ('Height', 2 ** 40),
                 ('TextColor', 2 ** 32), ('TextColor', -2 ** 31 - 1), ('TextColor', 2 ** 64),
                 ('Bold', 256), ('Bold', -1), ('Italic', 256), ('UnderlineType', -1), ('UnderlineType', 256),
                 ('RatioHangul', 256), ('RatioHangul', -1), ('SizeLatin', 256), ('SizeUser', -1),
                 ('SpacingHangul', 128), ('SpacingHangul', -129), ('OffsetHanja', 128), ('OffsetUser', -129))
        for item, value in cases:
            with self.subTest(item=item, value=value):
                self.assert_held_at_item(item, value, 'int')

    def test_a_value_inside_its_representation_that_is_not_the_style_s_is_refused(self) -> None:
        """No narrower usability range is imposed: a size of 0, a ratio of 0 or 255, a relative size of 255 or a spacing of 127 is a
        value of its type, so it differs from the style's and is refused."""
        cases = (('Height', 2 ** 31 - 1), ('Height', -2 ** 31), ('Height', 0),
                 ('TextColor', 0), ('TextColor', 2 ** 32 - 1), ('TextColor', -2 ** 31),
                 ('Bold', 255), ('Bold', 2), ('UnderlineType', 255),
                 ('RatioHangul', 0), ('RatioHangul', 255), ('SizeLatin', 255), ('SizeLatin', 0),
                 ('SpacingHangul', 127), ('SpacingHangul', -128), ('OffsetHanja', -128), ('OffsetUser', 127))
        for item, value in cases:
            with self.subTest(item=item, value=value):
                self.assert_refused(item, value)

    def test_a_colour_read_as_a_signed_32_bit_variant_still_passes(self) -> None:
        for colour, alias in ((4294967295, -1), (2 ** 31, -2 ** 31), (2 ** 32 - 0x336699, -0x336699)):
            with self.subTest(colour=colour, alias=alias):
                normal = {**RICH, 'TextColor': colour}
                _owned, inner, _trace, _start, outcome = self.answer('TextColor', alias, document=lambda normal=normal: rich_document(normal))
                self.assertTrue(outcome['ok'], outcome)
                self.assertEqual(inner.styles[0]['char'], canonical_char({**normal, 'Height': 1100}))

    def test_wrong_primitives_and_foreign_answers_hold_without_a_hook(self) -> None:
        cases = (('Height', True, 'true'), ('Height', False, 'false'), ('Height', 1000.0, 'float'), ('Height', math.nan, 'float'),
                 ('Height', One(1000), 'object'), ('Height', TouchyOne(1000), 'object'), ('Height', Hostile(), 'object'), ('Height', '1000', 'str'),
                 ('TextColor', True, 'true'), ('TextColor', float(0x336699), 'float'), ('SpacingHangul', TouchyOne(-5), 'object'),
                 ('FaceNameHangul', 0, 'int'), ('FaceNameHangul', TouchyOne(0), 'object'), ('FaceNameHangul', b'x', 'bytes'))
        for item, value, returned in cases:
            with self.subTest(item=item, value=returned):
                TOUCHED.clear()
                self.assert_held_at_item(item, value, returned)
                self.assertEqual(TOUCHED, [], 'no comparison, hash, truth, conversion or text hook of the answer was called')

    def test_a_raising_item_read_holds(self) -> None:
        for exc in (RuntimeError(SECRET), TypeError(SECRET), OverflowError(SECRET)):
            with self.subTest(exc=type(exc).__name__):
                owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault(ITEM, raising(exc), skip=BOUND.index('Height')), document=rich_document)
                self.assert_held(owned, inner, trace, outcome)
                self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False))

    def test_after_an_earlier_unsaved_edit_the_debt_is_kept_either_way(self) -> None:
        """The earlier edit applies 개요 1 to the last paragraph of styles_document: 개요 1's one plain body use to change."""
        request_of = lambda owned: update_request(owned, '개요 1', size_pt=13)  # noqa: E731
        owned, inner, trace, _start, outcome = self.answer('Height', 2 ** 31, document=styles_document, request_of=request_of, dirty_first=True)
        step = outcome['steps'][-1]
        self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
        self.assertNotIn('refusal_code', step)
        self.assertEqual(after_fault(trace), [])
        self.assertEqual(len(executes(inner)), 1, 'only the earlier edit was written')
        self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')
        self.assertEqual((self.diagnostic(outcome).get('native_site'), self.diagnostic(outcome).get('native_return')), ('char_shape.item', 'int'))
        owned, inner, trace, _start, outcome = self.answer('Height', 2 ** 31 - 1, document=styles_document, request_of=request_of, dirty_first=True)
        step = outcome['steps'][-1]
        self.assertEqual((step.get('refusal_code'), step['mutation_may_have_persisted']), ('style_shape_unverified', False), step)
        self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
        self.assertEqual(len(executes(inner)), 1)
        self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')

    def test_every_told_item_has_its_documented_type(self) -> None:
        """ParameterSetTable_2504 set 13, pages 16-18, for the items _bound_items reads."""
        documented = {'Height': 'I4', 'TextColor': 'UI4', 'Bold': 'UI1', 'Italic': 'UI1', 'UnderlineType': 'UI1'}
        for lang in ('Hangul', 'Latin', 'Hanja', 'Japanese', 'Other', 'Symbol', 'User'):
            documented.update({f'FaceName{lang}': 'BSTR', f'Ratio{lang}': 'UI1', f'Size{lang}': 'UI1', f'Spacing{lang}': 'I1', f'Offset{lang}': 'I1'})
        told = [item for item, _value in native_styles._bound_items(native_styles.parse_style_model(native_styles.document_text(  # noqa: SLF001
            rich_document().get_text_file('HWPML2X')))['styles'][0]['char'])]
        self.assertEqual(sorted(told), sorted(BOUND))
        self.assertEqual({item: native_styles._pit_type(item) for item in told}, {item: documented[item] for item in told})  # noqa: SLF001


class NoCreateSet(entry.PlainAction):
    """A legacy action with GetDefault and Execute but no CreateSet at all."""


class NoGetDefault:
    def CreateSet(self):  # noqa: N802
        raise AssertionError('never reached')

    def Execute(self, pset):  # noqa: N802
        raise AssertionError('never reached')


class NoExecute:
    def CreateSet(self):  # noqa: N802
        raise AssertionError('never reached')

    def GetDefault(self, pset):  # noqa: N802
        raise AssertionError('never reached')


class EmptySet:
    """A parameter set with no SetItem at all."""


class ActionOfAnEmptySet:
    def CreateSet(self):  # noqa: N802
        return EmptySet()

    def GetDefault(self, pset):  # noqa: N802
        raise AssertionError('never reached')

    def Execute(self, pset):  # noqa: N802
        raise AssertionError('never reached')


class EnteredLegacy(_Legacy):
    """The change's legacy family, read after the caret moved and the shape was read and written: a pure absence holds at its site."""

    def held(self, action, site: str, *, dirty_first: bool = False) -> None:
        document = styles_document if dirty_first else rich_document
        request_of = (lambda owned: update_request(owned, '개요 1', size_pt=13)) if dirty_first else to_11
        owned, inner, trace, _start, outcome = self.run_with('update', request_of, Fault('call:CreateAction', returning(action)), document=document, dirty_first=dirty_first)
        step = outcome['steps'][-1]
        self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
        self.assertNotIn('refusal_code', step, 'an entered absence is never the clean native_unavailable')
        self.assertEqual((self.diagnostic(outcome).get('native_site'), self.diagnostic(outcome).get('native_return')), (site, 'absent'), outcome)
        self.assertEqual(after_fault(trace), [], 'no caret return, census read or other call after the absent member')
        self.assertEqual(len(executes(inner)), 1 if dirty_first else 0)
        if dirty_first:
            self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')

    def test_a_purely_absent_action_member_holds(self) -> None:
        for action in (NoCreateSet(), NoGetDefault(), NoExecute()):
            with self.subTest(action=type(action).__name__):
                self.held(action, 'action.member')

    def test_a_purely_absent_set_item_holds(self) -> None:
        self.held(ActionOfAnEmptySet(), 'set.member')

    def test_after_an_earlier_unsaved_edit_the_debt_is_kept(self) -> None:
        self.held(NoCreateSet(), 'action.member', dirty_first=True)

    def test_a_purely_absent_create_action_holds_after_entry_and_refuses_before_it(self) -> None:
        """No owned wrapper can lack CreateAction purely, so the helper itself: entered, it holds at create_action.member; not entered
        (apply and create read it before the caret moves), it stays the clean native_unavailable."""
        class Bare:
            pass

        with self.assertRaises(NativeNumberingReadFailure) as held:
            native_styles._legacy(Bare(), native_styles._CHANGE_ACTION, ('SetItem',), entered=True)  # noqa: SLF001
        self.assertEqual((held.exception.native_site, held.exception.native_return), ('create_action.member', 'absent'))
        with self.assertRaises(NativeNumberingRefusal) as refused:
            native_styles._legacy(Bare(), native_styles._CREATE_ACTION, ('SetItem',))  # noqa: SLF001
        self.assertEqual(refused.exception.code, 'native_unavailable')

    def test_dynamic_faults_none_and_base_exceptions_still_hold(self) -> None:
        cases = (('get:CreateAction', raising(AttributeError('CreateAction')), 'create_action.member', 'raised'),
                 ('get:CreateAction', returning(None), 'create_action.member', 'not_callable'),
                 ('get:CreateAction', returning('CreateAction'), 'create_action.member', 'not_callable'),
                 (f'get:{L}Execute', returning(1), 'action.member', 'not_callable'),
                 (f'get:{L}CreateSet().SetItem', raising(RuntimeError(SECRET)), 'set.member', 'raised'))
        for when, effect, site, returned in cases:
            with self.subTest(when=when, returned=returned):
                owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault(when, effect), document=rich_document)
                self.assert_held(owned, inner, trace, outcome)
                self.assertEqual((self.diagnostic(outcome).get('native_site'), self.diagnostic(outcome).get('native_return')), (site, returned), outcome)
        for exc in (KeyboardInterrupt(SECRET), SystemExit(SECRET)):
            with self.subTest(exc=type(exc).__name__):
                owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault('get:CreateAction', raising(exc)), document=rich_document)
                self.assertFalse(outcome['ok'], outcome)
                self.assertNotIn('refusal_code', outcome['steps'][-1])
                self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(after_fault(trace), [])
                self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False))

    def test_create_still_reads_the_family_before_the_caret_moves(self) -> None:
        """C05's path: CreateAction and CreateSet before set_pos, unchanged."""
        _owned, _inner, trace, start, outcome = self.run_with('create', create_request)
        self.assertTrue(outcome['ok'], outcome)
        seen = trace[start:]
        self.assertLess(seen.index(f'call:{L}CreateSet'), seen.index('call:set_pos'))


if __name__ == '__main__':
    unittest.main()
