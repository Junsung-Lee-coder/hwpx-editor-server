"""Named styles: a change takes the style's whole character shape from IHwpObject.CharShape at its plain use, since StyleDirectEdit's
own defaults held none natively.

**Measured (C06, Source09, Hancom 13.0.0.3457, one host, the original seed).** Three public calls: open, the inventory (23 styles, 바탕글
at 10 pt with one plain body use) and style_update_exact of 바탕글 at 11 pt. The change was refused as style_variant_unsupported,
stopped before the edit, not held, not dirty. By the source path that refusal is raised only where ItemExist("CharShape") answered
False, after GetDefault answered 1 and the NameLocal SetItem answered None; Item, the size write, the set-back and Execute were not
reached. Why StyleDirectEdit's defaults held no CharShape sub-set is not known.

**The documented alternative.** HwpAutomation_2504 page 12: IHwpObject.CharShape gives the character shape of the selection, or of the
caret when nothing is selected, as a CharShape parameter set; an item whose value differs within the selection is absent, and a put
sets only the items it holds. The installed type library (gen_py f9ed176b) declares it DISPID 8, VT_DISPATCH, with a get and a put. The
CharShape set (ParameterSetTable_2504 set 13) has 67 items, the same as HCharShape of that type library, which spells OutlineType as
OutLineType. StyleDirectEdit's set, StyleItem (set 133), holds CharShape as a PIT_SET of that set. Hancom's answer in forum topic 2354
(post 5) says a sub-set is a copy under Automation, set back with SetItem; post 2 quotes the official warning that no action may be
created while another awaits its Execute.

**The proposed mechanism, offline only.** With the caret in the style's plain body use and nothing selected, and before StyleDirectEdit
is made, the change reads that set, checks every documented item is there, checks the items it can read off the document text
(size, colour, bold, italic, underline, and per language font, ratio, spacing, relative size and offset) equal the style's, writes
only the requested items into it and sets it back on StyleDirectEdit's set before its one Execute. A set without every item, or one
whose items are not the style's, is refused as style_shape_unverified before anything is written. Native: NOT RUN.

Through the real bundle validation, package dispatch, owned service, engine, public projection and CLI normaliser, over
tests.native_styles_fakes. Not Hancom evidence.
"""
from __future__ import annotations

import copy
import json
import unittest

from app import style_rules
from local_cli_v1.output_parser import normalize_command_bundle
from tests import test_native_styles_entry as entry
from tests.native_format_fakes import char
from tests.native_styles_fakes import CHAR_SHAPE_ITEMS, StyleHwp, _LegacySet, canonical_char, char_shape_items, styles_document
from tests.test_native_styles import executes
from tests.test_native_styles_copy import FIELD_BEGIN, FIELD_END, text_and_controls
from tests.test_native_styles_filled import RICH, SAME_FONTS, TEXTS, UNBOUND_ITEMS, problem, reset_unbound, rich_document, run_on, to_11
from tests.test_native_styles_legacy import SECRET, _Legacy, update_request
from tests.test_native_styles_repairs import Fault, after_fault, bundle, raising, returning

L = entry.LEGACY
S = 'CharShape.'
# The items a change reads to bind the set to the style: their values off the document text.
BOUND = ('Height', 'TextColor', 'Bold', 'Italic', 'UnderlineType',
         *(f'{kind}{lang}' for lang in ('Hangul', 'Latin', 'Hanja', 'Japanese', 'Other', 'Symbol', 'User') for kind in ('FaceName', 'Ratio', 'Spacing', 'Size', 'Offset')))
def seed_like() -> StyleHwp:
    """바탕글 as the seed has it, 함초롬바탕 at 10 pt with every other item at its default, in one plain body use with a click-here field."""
    return rich_document(char())


class Measured(unittest.TestCase):
    """The fake itself: StyleDirectEdit's defaults as C06 found them, and the documented CharShape read."""

    def test_style_direct_edit_s_defaults_hold_no_character_shape_after_name_local(self) -> None:
        hwp = seed_like()
        hwp.cursor = (0, 1, 2)
        action = hwp.CreateAction('StyleDirectEdit')
        pset = action.CreateSet()
        self.assertEqual(action.GetDefault(pset), 1)
        self.assertIsNone(pset.SetItem('NameLocal', '바탕글'))
        self.assertIs(pset.ItemExist('CharShape'), False, 'as C06 found it, by the source path')
        added = hwp.CreateAction('StyleAddNew')
        aset = added.CreateSet()
        added.GetDefault(aset)
        self.assertIs(aset.ItemExist('CharShape'), True, "StyleAddNew's defaults are not measured; this fake fills them as before")

    def test_the_char_shape_read_is_whole_and_a_new_copy_each_time(self) -> None:
        hwp = rich_document()
        hwp.cursor = (0, 1, 2)
        first, second = hwp.CharShape, hwp.CharShape
        self.assertEqual(sorted(first.items), sorted(CHAR_SHAPE_ITEMS))
        self.assertEqual({key: value for key, value in first.items.items() if key != 'BorderFill'},
                         {key: value for key, value in char_shape_items(RICH).items() if key != 'BorderFill'})
        self.assertIsNot(first, second)
        first.SetItem('Height', 1100)
        self.assertEqual(second.Item('Height'), 1000)
        self.assertEqual(hwp.styles[0]['char'], RICH, 'writing into the read set changes nothing in the document')
        self.assertEqual(hwp.char_shape_puts, [])
        hwp.char_shape_copies = False
        self.assertIs(hwp.CharShape, hwp.CharShape, 'the knob: one object every time')

    def test_a_selection_drops_the_items_that_differ_within_it(self) -> None:
        hwp = styles_document()
        hwp.selection = (0, 2, 0, 0, 2, 9)  # 본문 둘째 굵은말 끝: one bold word inside 본문
        got = hwp.CharShape
        self.assertNotIn('Bold', got.items)
        self.assertIn('Height', got.items)

    def test_a_whole_set_over_the_factory_shape_is_the_set_s_shape(self) -> None:
        hwp = rich_document()
        for props in (RICH, SAME_FONTS, char(), char(Bold=True, Underline='Bottom')):
            items = char_shape_items(props)
            self.assertEqual(hwp._char_with(dict(hwp.factory_char), items), canonical_char(props))  # noqa: SLF001
            partial = {key: value for key, value in items.items() if key not in ('ShadeColor', 'UseKerning')}
            if props is RICH:
                self.assertNotEqual(hwp._char_with(dict(hwp.factory_char), partial), canonical_char(props), 'a partial set resets what it lacks')  # noqa: SLF001


class Change(_Legacy):
    """The C06 request, now: through the real owned service, on the condition C06 measured."""

    def change(self, *faults: Fault, document=rich_document, **fields):
        return self.run_with('update', lambda owned: to_11(owned, **fields), *faults, document=document)

    def assert_changed(self, outcome: dict, inner: StyleHwp, before: StyleHwp, trace: list, start: int, normal: dict) -> None:
        self.assertTrue(outcome['ok'], outcome)
        result = outcome['steps'][-1]['result']
        wanted = canonical_char({**normal, 'Height': 1100})
        self.assertEqual(inner.styles[0]['char'], wanted)
        self.assertEqual({key: value for key, value in inner.styles[0].items() if key != 'char'},
                         {key: value for key, value in before.styles[0].items() if key != 'char'})
        self.assertEqual(inner.styles[1:], before.styles[1:], 'every other style')
        self.assertTrue(all(atom['cs'] == wanted for atom in inner.lists[0][1]['atoms']), 'the field pair and the place marker too')
        self.assertEqual(text_and_controls(inner), text_and_controls(before))
        self.assertEqual([item for item in inner.every_paragraph() if item.get('style', 0) != 0], [item for item in before.every_paragraph() if item.get('style', 0) != 0])
        self.assertEqual(inner.font_info, before.font_info)
        written = inner.get_text_file('HWPML2X')
        self.assertEqual((written.count(FIELD_BEGIN), written.count(FIELD_END)), (1, 1))
        old, new = result['style_before'], result['style_after']
        self.assertEqual(new['character'], {**old['character'], 'size_pt': 11.0})
        self.assertEqual({key: value for key, value in new.items() if key not in {'hash', 'character'}},
                         {key: value for key, value in old.items() if key not in {'hash', 'character'}})
        self.assertEqual((result['result']['status'], result['result']['native_actions'], result['result']['affected']['total']), ('updated', ['Execute:StyleDirectEdit'], 1))
        seen = trace[start:]
        calls = [item for item in seen if item.startswith('call:') and item != 'call:get_text_file' and 'pos' not in item]
        reads = [item for item in calls if item.startswith(f'call:{S}')]
        self.assertEqual(calls[:len(reads)], reads, 'the shape is read, checked and written before anything else')
        self.assertEqual([item for item in reads if item != f'call:{S}ItemExist' and item != f'call:{S}Item'], [f'call:{S}SetItem'], 'one requested write')
        self.assertEqual(reads.count(f'call:{S}ItemExist'), len(CHAR_SHAPE_ITEMS), 'every documented item asked for')
        self.assertEqual(reads.count(f'call:{S}Item'), len(BOUND), 'every item the document text tells')
        self.assertEqual(calls[len(reads):], ['call:CreateAction', f'call:{L}CreateSet', f'call:{L}GetDefault', f'call:{L}CreateSet().SetItem',
                                              f'call:{L}CreateSet().SetItem', f'call:{L}Execute'], 'NameLocal, the set-back, one Execute')
        self.assertLess(seen.index('get:CharShape'), seen.index('call:CreateAction'), 'read before the action is made')
        self.assertEqual(seen.count('call:CreateAction'), 1, 'no other action while StyleDirectEdit awaits its Execute')
        self.assertEqual([item for item in seen if 'CreateItemSet' in item or item.startswith((f'call:{L}CreateSet().ItemExist', f'call:{L}CreateSet().Item'))], [],
                         "StyleDirectEdit's defaults are neither asked for a character shape nor given a made one")
        self.assertEqual([item for item in seen if item == 'set:CharShape'], [], 'no put')
        self.assertEqual(inner.char_shape_puts, [])
        recorded = executes(inner)[0][4]
        whole = {key: (('set', 'BorderFill') if key == 'BorderFill' else value) for key, value in char_shape_items(normal).items()}
        self.assertEqual(recorded, {**whole, 'Height': 1100}, 'the executed CharShape: the whole shape read, the size alone changed')

    def test_c06s_request_changes_the_seed_like_바탕글_to_11_pt(self) -> None:
        hwp = seed_like()
        before = copy.deepcopy(hwp)
        _owned, inner, trace, start, outcome = self.change(document=lambda: hwp)
        self.assertFalse(inner.direct_edit_char_shape, "on C06's condition")
        self.assert_changed(outcome, inner, before, trace, start, char())

    def test_a_rich_바탕글_keeps_every_other_property_under_copies_and_references(self) -> None:
        for shape_copies in (True, False):
            for set_copies in (True, False):
                with self.subTest(char_shape_copies=shape_copies, legacy_copies=set_copies):
                    hwp = rich_document()
                    hwp.char_shape_copies, hwp.legacy_copies = shape_copies, set_copies
                    before = copy.deepcopy(hwp)
                    _owned, inner, trace, start, outcome = self.change(document=lambda: hwp)
                    self.assert_changed(outcome, inner, before, trace, start, RICH)

    def test_a_filled_or_unfilled_default_shape_is_not_used(self) -> None:
        for unfilled in (False, True):
            with self.subTest(unfilled=unfilled):
                hwp = rich_document()
                hwp.direct_edit_char_shape, hwp.legacy_subsets_unfilled = True, unfilled
                before = copy.deepcopy(hwp)
                _owned, inner, trace, start, outcome = self.change(document=lambda: hwp)
                self.assert_changed(outcome, inner, before, trace, start, RICH)

    def test_either_spelling_of_the_outline_item_passes(self) -> None:
        hwp = rich_document()
        hwp.char_shape_spelling = 'OutlineType'
        _owned, _inner, trace, start, outcome = self.change(document=lambda: hwp)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(trace[start:].count(f'call:{S}ItemExist'), len(CHAR_SHAPE_ITEMS) + 1, "the type library's spelling first, then the manual's")

    def test_other_requested_properties_are_written_into_the_same_set(self) -> None:
        hwp = rich_document()
        _owned, inner, trace, start, outcome = self.change(document=lambda: hwp, bold=True, text_color='#FF0000')
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(inner.styles[0]['char'], canonical_char({**RICH, 'Height': 1100, 'Bold': True, 'TextColor': 255}))
        self.assertEqual(trace[start:].count(f'call:{S}SetItem'), 3)


class Unverified(_Legacy):
    """A read set that is not the style's whole shape is refused before anything is written, with the caret given back."""

    def refused(self, document, *, dirty_first: bool = False, request_of=to_11) -> dict:
        owned, inner, trace = self.owned(document, dirty_first=dirty_first)
        caret = inner.cursor
        request = request_of(owned)
        start = len(trace)
        outcome = bundle(owned, **request)
        step = outcome['steps'][-1]
        self.assertEqual(step.get('refusal_code'), 'style_shape_unverified', step)
        self.assertIn('nothing was written', step.get('refusal_message', ''))
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
        self.assertEqual(len(executes(inner)), 1 if dirty_first else 0, 'nothing more is written')
        seen = trace[start:]
        after = seen[seen.index('get:CharShape'):]
        for name in ('call:CreateAction', f'call:{S}SetItem', f'call:{L}Execute'):
            self.assertNotIn(name, after, 'no action made and no item written')
        self.assertIn('call:set_pos', after, 'the caret is given back')
        self.assertIn('call:get_text_file', after, 'and the document read again')
        self.assertEqual(inner.cursor, caret)
        self.assertEqual(inner.char_shape_puts, [])
        if dirty_first:
            self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')
        return step

    def test_a_set_without_every_documented_item(self) -> None:
        for dropped in (('BorderFill',), ('SizeLatin',), ('FaceNameUser',), ('StrikeOutColor',), ('OutLineType',)):
            with self.subTest(dropped=dropped):
                def document(dropped=dropped):
                    hwp = rich_document()
                    hwp.char_shape_drop = dropped
                    return hwp
                step = self.refused(document)
                self.assertIn('OutlineType' if dropped == ('OutLineType',) else dropped[0], step['refusal_message'], 'named by its documented item ID')

    def test_a_shape_that_is_not_the_style_s(self) -> None:
        cases = {'another size': lambda owner, source: {**source, 'Height': 1300},
                 'another font in one language': lambda owner, source: canonical_char({**source, 'Fonts': {**source['Fonts'], 'Latin': '함초롬바탕'}}),
                 'bold': lambda owner, source: {**source, 'Bold': True},
                 'another ratio': lambda owner, source: canonical_char({**source, 'Ratios': {**source['Ratios'], 'User': 80}}),
                 'another colour': lambda owner, source: {**source, 'TextColor': 0},
                 'the factory shape': lambda owner, source: dict(owner.factory_char),
                 "본문's shape": lambda owner, source: dict(owner.styles[1]['char'])}
        for name, foreign in cases.items():
            with self.subTest(shape=name):
                def document(foreign=foreign):
                    hwp = rich_document()
                    hwp.char_shape_from = foreign
                    return hwp
                step = self.refused(document)
                self.assertIn("is not the style's own", step['refusal_message'])

    def test_after_an_earlier_dirty_edit_the_debt_is_kept(self) -> None:
        """The earlier edit applies 개요 1 to the last paragraph of styles_document: 개요 1's one plain body use to change."""
        def document():
            hwp = styles_document()
            hwp.char_shape_drop = ('BorderFill',)
            return hwp
        self.refused(document, dirty_first=True, request_of=lambda owned: update_request(owned, '개요 1', size_pt=13))


class UnboundDifference(_Legacy):
    """Items the binding cannot read off the document text are still proven after the action: a read set that differs only there
    reaches Execute, and the full proof holds the change with what differed."""

    def test_the_proof_holds_what_the_binding_does_not_see(self) -> None:
        def document():
            hwp = rich_document()
            hwp.char_shape_from = reset_unbound
            return hwp

        owned, inner, trace, _start, outcome = self.run_with('update', to_11, document=document)
        step = outcome['steps'][-1]
        self.assertTrue(step['mutation_may_have_persisted'], step)
        self.assertTrue(step.get('native_custody_held'), step)
        found = problem(self, step)
        self.assertEqual((found['predicate'], found['name'], found['fields']), ('changed_style_wrong', '바탕글', ['character']))
        self.assertEqual(found['character_items'], UNBOUND_ITEMS)
        self.assertEqual((found['size_expected'], found['size_found'], found['unrequested_changed']), (11.0, 11.0, True))
        self.assertEqual(len(executes(inner)), 1)
        count = len(trace)
        with self.assertRaises(Exception):
            bundle(owned, op='style_inventory')
        self.assertEqual(trace[count:], [], 'no native call after the hold')


class EntryFaults(_Legacy):
    """The CharShape read itself, once the edit has entered native navigation: each fault holds at once with no further SDK call."""

    def test_the_char_shape_getter(self) -> None:
        for name, effect, returned in (('raises', raising(RuntimeError(SECRET)), 'raised'), ('attribute error', raising(AttributeError('CharShape')), 'raised'),
                                       ('none', returning(None), 'none'), ('plain', returning(1), 'int'), ('text', returning('CharShape'), 'str')):
            with self.subTest(effect=name):
                owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault('get:CharShape', effect), document=rich_document)
                self.assert_held(owned, inner, trace, outcome)
                self.assertEqual((self.diagnostic(outcome).get('native_site'), self.diagnostic(outcome).get('native_return')), ('char_shape.read', returned))
                self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False))

    def test_a_foreign_object_without_the_set_s_methods(self) -> None:
        class Plain:
            pass

        owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault('get:CharShape', returning(Plain())), document=rich_document)
        self.assert_held(owned, inner, trace, outcome)
        self.assertEqual((self.diagnostic(outcome).get('native_site'), self.diagnostic(outcome).get('native_return')), ('char_shape.member', 'absent'))

    def test_a_font_name_that_is_not_text_and_a_number_that_is_a_bool(self) -> None:
        for name, skip, value, shape in (('Height', 0, True, 'true'), ('Height', 0, 1100.0, 'float'), ('FaceNameHangul', BOUND.index('FaceNameHangul'), 0, 'int'),
                                         ('FaceNameHangul', BOUND.index('FaceNameHangul'), b'x', 'bytes')):
            with self.subTest(item=name, value=shape):
                owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault(f'call:{S}Item', returning(value), skip=skip), document=rich_document)
                self.assert_held(owned, inner, trace, outcome)
                self.assertEqual((self.diagnostic(outcome).get('native_site'), self.diagnostic(outcome).get('native_return')), ('char_shape.item', shape))

    def test_a_base_exception_at_the_read_holds(self) -> None:
        for exc in (KeyboardInterrupt(SECRET), SystemExit(SECRET)):
            with self.subTest(exc=type(exc).__name__):
                owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault('get:CharShape', raising(exc)), document=rich_document)
                self.assertFalse(outcome['ok'], outcome)
                self.assertNotIn('refusal_code', outcome['steps'][-1])
                self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(after_fault(trace), [])
                self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False))

    def test_a_wrapper_with_no_char_shape_at_all_holds_once_the_caret_stands_at_the_use(self) -> None:
        """The seed's own capture through a recorded object with no CharShape member at all: the change reads it only after placing
        the caret, so even a pure absence is an unknown outcome there, not a refusal."""
        from tests.test_native_styles_before import RecordedHwp, captured, listed, run
        bare = type('Bare', (), {key: value for key, value in RecordedHwp.__dict__.items() if key not in {'CharShape', '__dict__', '__weakref__'}})
        hwp = bare(captured('native_fields/before.hwpml2x'))
        found = listed(hwp, name='바탕글')['styles'][0]
        outcome = run(hwp, op='style_update_exact', style_name='바탕글', expected_style_hash=found['hash'], expected_uses=found['uses']['total'],
                      expected_uses_hash=found['uses']['hash'], all_uses=True, size_pt=11, confirm_layout=True)
        step = outcome['steps'][0]
        self.assertFalse(outcome['ok'], outcome)
        self.assertNotIn('refusal_code', step)
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertEqual([call[0] for call in hwp.calls if call[0] != 'get_text_file'], ['set_pos'], 'nothing after the absent read')
        self.assertEqual((step['failure_diagnostic'].get('native_site'), step['failure_diagnostic'].get('native_return')), ('char_shape.read', 'absent'))

    def test_a_colour_is_compared_by_its_32_bits(self) -> None:
        from app import native_styles
        self.assertTrue(native_styles._same_item('TextColor', -1, 4294967295))  # noqa: SLF001
        self.assertTrue(native_styles._same_item('TextColor', 255, 255))  # noqa: SLF001
        self.assertFalse(native_styles._same_item('TextColor', 1 << 32, 0), 'beyond 32 bits is no COLORREF')  # noqa: SLF001
        self.assertFalse(native_styles._same_item('Height', -1, 4294967295), 'only a colour is read as 32 bits')  # noqa: SLF001

    def test_the_new_sites_are_labels(self) -> None:
        self.assertLessEqual({'char_shape.read', 'char_shape.member', 'char_shape.item_exist', 'char_shape.item'}, style_rules.NATIVE_SITES)


class Reporting(unittest.TestCase):
    def test_the_refusal_reaches_the_cli_by_its_code(self) -> None:
        hwp = rich_document()
        hwp.char_shape_drop = ('BorderFill',)
        outcome = run_on(self, hwp)
        step = outcome['steps'][-1]
        self.assertEqual(step['refusal_code'], 'style_shape_unverified')
        normalized = normalize_command_bundle(outcome)
        self.assertEqual(normalized['steps'][0].get('refusal_code', normalized['steps'][0].get('extras', {}).get('refusal_code')), 'style_shape_unverified')
        dumped = json.dumps(outcome, ensure_ascii=False)
        for text in TEXTS:
            self.assertNotIn(text, dumped)


if __name__ == '__main__':
    unittest.main()
