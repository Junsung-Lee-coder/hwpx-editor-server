"""Named styles: a change keeps every character property but the requested ones (Source09's filled sub-set, re-pointed in Source10).

Source10: C06 (Source09, Hancom 13.0.0.3457, one host, the original seed) found StyleDirectEdit's defaults without a CharShape
sub-set: by the source path ItemExist("CharShape") answered False after GetDefault and the NameLocal SetItem, and the change was
refused before the edit. The change now takes the style's whole shape from IHwpObject.CharShape at its plain use, before
StyleDirectEdit is made (tests.test_native_styles_snapshot). The change's oracles below keep their intent on that set: the size
alone changes, a shape never set back changes nothing, a shape without every item is refused before any write, and each first
fault holds; where a Source09 oracle injected its fault at the filled sub-set, it is injected at the read set now. The create's
filled path and the fake's own sub-set semantics are unchanged.

C03 (Source08, Hancom 13.0.0.3457, one host) requested 바탕글 at 11 pt: three public calls, then a hold after StyleDirectEdit's
Execute answered True. Its style_proof had one problem, changed_style_wrong for 바탕글 with the field character only. Neither the
text after the action nor its model was kept, so which items differed, and whether Height became 1100, is not known.

That source made the change's CharShape sub-set with CreateItemSet("CharShape", "CharShape") and set only Height in it. Hancom's
answer in forum topic 1044 post 6 (2023-11-09, about a PageBorder FillAttr sub-set): a sub-set made with CreateItemSet needs every
one of its child items set; to change some, edit the sub-set GetDefault filled, which Item gives. Topic 2354 post 5: under
Automation that sub-set is a copy, set back on its parent with SetItem. Topic 1552 post 4's StyleDirectEdit example uses
CreateItemSet. The change now reads ItemExist("CharShape") and Item("CharShape") after GetDefault, sets only the requested items in
that copy, sets the copy back once and executes once, as the create already did. Defaults without a character shape are refused
before anything is written.

tests.native_styles_fakes models the mechanism that answer describes: a sub-set made with CreateItemSet starts from a factory shape
(factory_char), not from the style's. That is a model, not C03's after-state. 바탕글 here differs from that factory shape in
properties no request names: a font with TYPEINFO for each of the 7 languages, ratios, spacings, relative sizes, offsets, a shade
colour, kerning, font spacing, a symbol mark, italic, an underline and a colour, in its one plain body use, which holds a click-here
field pair and a place marker.

A held proof also says what of the character shape differed (size expected and found, the differing items by fixed label and their
count, and whether any of them was not requested), from the model the proof already read: no further SDK call.

Through the real bundle validation, package dispatch, owned service, engine, public projection, runtime journal and CLI normaliser.
Not Hancom evidence.
"""
from __future__ import annotations

import copy
import json
import math
import unittest
from unittest.mock import patch

from app import local_cli_runtime as rt, local_cli_service as svc, native_styles, style_rules
from app.command_packages.runtime import get_command_package_registry
from app.local_cli_service_support import LocalCliServiceError
from local_cli_v1.output_parser import normalize_command_bundle
from tests import test_cli_failure_recovery as recovery
from tests import test_native_styles_entry as entry
from tests.native_format_fakes import bookmark, char, control
from tests.native_styles_fakes import StyleHwp, _char_items, _LegacySet, area, canonical_char, char_shape_items, default_styles, styled, styles_document
from tests.test_native_styles import CREATE, executes
from tests.test_native_styles_copy import FIELD_BEGIN, FIELD_END, text_and_controls
from tests.test_native_styles_legacy import SECRET, TOUCHED, Foreign, Hostile, PlainSub, _Legacy, create_request, update_request
from tests.test_native_styles_repairs import TYPEINFO, Fault, after_fault, bundle, raising, returning
from tests.test_repair_runtime_safety import owned_doc

L = entry.LEGACY
LANGS = ('Hangul', 'Latin', 'Hanja', 'Japanese', 'Other', 'Symbol', 'User')
FONTS = dict(zip(LANGS, ('함초롬돋움', '본문 라틴', '한자 바탕', '일본 명조', '기타 글꼴', '기호 글꼴', '사용자 글꼴')))
# 바탕글's character shape: nothing a request names differs from the factory shape but its size, which a change sets.
RICH = char(TextColor=0x336699, Italic=True, Underline='Bottom', UnderlineColor=0x112233, Font=FONTS['Hangul'], Fonts=dict(FONTS),
            Ratios={'Hangul': 95, 'Latin': 90, 'Hanja': 105}, Spacings={'Hangul': -5, 'Latin': 3}, RelSizes={'Latin': 110, 'Symbol': 90},
            Offsets={'Hanja': 2, 'User': -1}, ShadeColor=0xEEEEEE, UseKerning=True, UseFontSpace=True, SymMark=1)
# Only properties outside the factory shape's fonts: a factory reset leaves the fonts, as C03's field (character, not fonts) allows.
SAME_FONTS = char(Ratios={'Hangul': 95, 'Latin': 90}, Spacings={'Hangul': -5}, ShadeColor=0xEEEEEE, UseKerning=True)
TEXTS = ('본문 첫 문단', '성명', '기존성명', '끝', '개요 문단', '머리 본문', *FONTS.values(), '함초롬바탕')
KEYS = sorted(lang.lower() for lang in LANGS)  # The language keys of the document text, in the order the facts name them.
# What a factory reset of RICH differs in from RICH at 11 pt, by fixed label: every one of them unrequested.
RESET_ITEMS = ['attr_shadecolor', 'attr_symmark', 'attr_textcolor', 'attr_usefontspace', 'attr_usekerning', 'italic', 'underline',
               *(f'font_{key}' for key in KEYS), 'child_charoffset', 'child_charspacing', 'child_ratio', 'child_relsize',
               *(f'font_content_{key}' for key in KEYS)]


def rich_document(normal: dict | None = None) -> StyleHwp:
    """본문 first (that paragraph carries the section definition), then 바탕글's one plain body use, holding a click-here field pair
    and a place marker in 바탕글's shape, then 개요 1; 본문 in the header. Each font has TYPEINFO of its own."""
    normal = dict(normal or RICH)
    styles = default_styles()
    styles[0]['char'] = dict(normal)
    body = [styled('본문 첫 문단', 1, para_shape=styles[1]['para']),
            styled((('성명: ', normal), control(FIELD_BEGIN, '%clk', cs=dict(normal)), ('기존성명', normal), control(FIELD_END, '', cs=dict(normal)),
                    (' 끝 ', normal), bookmark('표시', cs=dict(normal))), 0, para_shape=styles[0]['para']),
            styled((('개요 문단', styles[2]['char']),), 2, para_shape=styles[2]['para'])]
    hwp = StyleHwp(body, styles=styles, areas=[area('header', styled('머리 본문', 1, para_shape=styles[1]['para']))])
    hwp.font_info = {name: {**TYPEINFO, 'Weight': str(3 + number)} for number, name in enumerate(FONTS.values())}
    hwp.font_info['함초롬바탕'] = dict(TYPEINFO)
    return hwp


def to_11(owned, **fields) -> dict:
    return update_request(owned, '바탕글', size_pt=11, **fields)


# Items a change cannot read off the document text to bind the read set to the style, put back to the factory shape: the read set
# then passes the binding, and the full proof after the action holds what differs.
UNBOUND_RESET = {'ShadeColor': 4294967295, 'UseKerning': False, 'UseFontSpace': False, 'SymMark': 0}
UNBOUND_ITEMS = ['attr_shadecolor', 'attr_symmark', 'attr_usefontspace', 'attr_usekerning']
S = 'CharShape.'


def reset_unbound(owner: StyleHwp, source: dict) -> dict:
    return canonical_char({**source, **UNBOUND_RESET})


def unbound_document(normal: dict | None = None) -> StyleHwp:
    hwp = rich_document(normal)
    hwp.char_shape_from = reset_unbound
    return hwp


def made_instead(real):
    """Item answers a sub-set made empty, as CreateItemSet makes one, instead of the copy of the filled one."""
    real()
    return _LegacySet('CharShape')


def problem(test: unittest.TestCase, step: dict) -> dict:
    proof = step.get('style_proof')
    test.assertIsNotNone(proof, step)
    test.assertEqual(style_rules.style_proof_facts(proof), proof, 'exactly of the fixed shape')
    dumped = json.dumps(proof, ensure_ascii=False)
    for text in TEXTS:
        test.assertNotIn(text, dumped, 'no document text or font name in the facts')
    test.assertEqual(len(proof['problems']), 1, proof)
    return proof['problems'][0]


class Mechanism(unittest.TestCase):
    """The fake itself: what a filled sub-set and a made one carry into StyleDirectEdit."""

    def defaults(self, hwp: StyleHwp) -> _LegacySet:
        hwp.cursor = (0, 1, 2)
        hwp.direct_edit_char_shape = True  # Source09's model of StyleDirectEdit's defaults; C06 found none (tests.test_native_styles_snapshot).
        action = hwp.CreateAction('StyleDirectEdit')
        pset = action.CreateSet()
        self.assertEqual(action.GetDefault(pset), 1)
        return pset

    def test_a_filled_sub_set_carries_the_style_and_a_made_one_the_factory_shape(self) -> None:
        hwp = rich_document()
        pset = self.defaults(hwp)
        self.assertTrue(pset.ItemExist('CharShape'))
        filled = pset.Item('CharShape')
        self.assertEqual(filled.items, _char_items(RICH), "GetDefault's items: every language's own font")
        filled.SetItem('Height', 1100)
        self.assertEqual(hwp._sub_char(pset, {}), RICH, 'a copy changed but never set back changes nothing')  # noqa: SLF001
        pset.SetItem('CharShape', filled)
        self.assertEqual(hwp._sub_char(pset, {}), {**RICH, 'Height': 1100}, 'the filled copy set back: Height alone changes')  # noqa: SLF001
        pset = self.defaults(hwp)
        made = pset.CreateItemSet('CharShape', 'CharShape')
        made.SetItem('Height', 1100)
        pset.SetItem('CharShape', made)
        self.assertEqual(hwp._sub_char(pset, {}), {**char(), 'Height': 1100}, "a made sub-set: the factory shape, not 바탕글's")  # noqa: SLF001

    def test_the_document_text_carries_every_property_and_font(self) -> None:
        text = rich_document().get_text_file('HWPML2X')
        for part in ('ShadeColor="15658734"', 'SymMark="1"', 'TextColor="3368601"', 'UseFontSpace="true"', 'UseKerning="true"', '<ITALIC/>',
                     'Type="Bottom"', 'Color="1122867"', '<RATIO Hangul="95" Hanja="105" Japanese="100" Latin="90"', 'Weight="9"'):
            self.assertIn(part, text)
        for name in FONTS.values():
            self.assertIn(f'Name="{name}" Type="ttf"><TYPEINFO', text)


class Retired(unittest.TestCase):
    def test_the_made_sub_set_s_site_is_no_label_any_more(self) -> None:
        """No command makes a sub-set, so its site is not a label a hold may carry; the sites of the filled one are."""
        self.assertNotIn('create_item_set.CharShape', style_rules.NATIVE_SITES)
        self.assertLessEqual({'item_exist.CharShape', 'item.CharShape', 'subset.member', 'subset_set_item.Height', 'set_item.CharShape'}, style_rules.NATIVE_SITES)


class FilledCopy(_Legacy):
    """Through the real service: the read whole shape keeps everything but Height; one not the style's is refused or held."""

    def change(self, *faults: Fault, document=rich_document, **fields):
        return self.run_with('update', lambda owned: to_11(owned, **fields), *faults, document=document)

    def held(self, outcome: dict) -> dict:
        step = outcome['steps'][-1]
        self.assertFalse(outcome['ok'], outcome)
        self.assertTrue(step['mutation_may_have_persisted'], step)
        self.assertNotIn('refusal_code', step)
        self.assertTrue(step.get('native_custody_held'), step)
        return step

    def no_follow_on(self, owned, trace: list) -> None:
        count = len(trace)
        with self.assertRaises(LocalCliServiceError) as caught:
            bundle(owned, op='style_inventory')
        self.assertIn('native_custody_held', str(caught.exception))
        self.assertEqual(trace[count:], [], 'no native call of any kind after the hold')

    def test_size_11_changes_only_height_on_the_style_its_use_and_nothing_else(self) -> None:
        for copies in (True, False):
            with self.subTest(copies=copies):
                hwp = rich_document()
                hwp.legacy_copies = copies
                before = copy.deepcopy(hwp)
                owned, inner, trace, start, outcome = self.change(document=lambda: hwp)
                self.assertTrue(outcome['ok'], outcome)
                result = outcome['steps'][-1]['result']
                wanted = {**RICH, 'Height': 1100}
                self.assertEqual(inner.styles[0]['char'], wanted)
                self.assertEqual({key: value for key, value in inner.styles[0].items() if key != 'char'},
                                 {key: value for key, value in before.styles[0].items() if key != 'char'})
                self.assertEqual(inner.styles[1:], before.styles[1:], 'every other style')
                use = inner.lists[0][1]
                self.assertTrue(all(atom['cs'] == wanted for atom in use['atoms']), 'the field pair and the place marker too')
                self.assertEqual(text_and_controls(inner), text_and_controls(before), 'every text and control')
                self.assertEqual([item for item in inner.every_paragraph() if item.get('style', 0) != 0],
                                 [item for item in before.every_paragraph() if item.get('style', 0) != 0])
                self.assertEqual(inner.font_info, before.font_info)
                written = inner.get_text_file('HWPML2X')
                self.assertEqual((written.count(FIELD_BEGIN), written.count(FIELD_END), written.count('<BOOKMARK Name="표시"/>')), (1, 1, 1))
                old, new = result['style_before'], result['style_after']
                self.assertEqual(new['character'], {**old['character'], 'size_pt': 11.0})
                self.assertEqual(old['character']['font_family'], {lang.lower(): name for lang, name in FONTS.items()}, 'a font per language')
                self.assertEqual({key: value for key, value in new.items() if key not in {'hash', 'character'}},
                                 {key: value for key, value in old.items() if key not in {'hash', 'character'}})
                self.assertEqual((result['result']['status'], result['result']['native_actions'], result['result']['affected']['total']),
                                 ('updated', ['Execute:StyleDirectEdit'], 1))
                seen = trace[start:]
                order = [item for item in seen if item.startswith('call:') and item != 'call:get_text_file' and 'pos' not in item
                         and item not in (f'call:{S}ItemExist', f'call:{S}Item')]
                self.assertEqual(order, [f'call:{S}SetItem', 'call:CreateAction', f'call:{L}CreateSet', f'call:{L}GetDefault', f'call:{L}CreateSet().SetItem',
                                         f'call:{L}CreateSet().SetItem', f'call:{L}Execute'],
                                 'one requested item in the read shape, then the action, NameLocal, the shape set back once, one Execute')
                self.assertEqual([item for item in seen if 'CreateItemSet' in item], [], 'no sub-set is made')
                whole = {key: (('set', 'BorderFill') if key == 'BorderFill' else value) for key, value in char_shape_items(RICH).items()}
                self.assertEqual(executes(inner)[0][4], {**whole, 'Height': 1100}, "the executed CharShape: the read whole shape, Height alone changed")

    def test_a_factory_shape_read_in_place_of_the_style_s_is_refused_before_any_write(self) -> None:
        """Source09's counterexample was the old made sub-set at the filled one's place, which reset every unrequested property and
        held after Execute. The same factory shape given as the read set now differs from 바탕글 where the binding reads it, and is
        refused before the action is made."""
        hwp = rich_document()
        hwp.char_shape_from = lambda owner, source: dict(owner.factory_char)
        owned, inner, trace, start, outcome = self.change(document=lambda: hwp)
        step = outcome['steps'][-1]
        self.assertEqual((step.get('refusal_code'), step['mutation_may_have_persisted']), ('style_shape_unverified', False), step)
        self.assertEqual(inner.styles[0]['char'], RICH)
        self.assertEqual(executes(inner), [])
        self.assertNotIn('call:CreateAction', trace[start:])

    def test_with_the_fonts_kept_the_reset_holds_by_the_character_field_alone(self) -> None:
        """The predicate and field C03 measured (changed_style_wrong, character), from a read set reset where the binding does not read;
        C03's own items are not known."""
        owned, inner, trace, _start, outcome = self.change(document=lambda: unbound_document(SAME_FONTS))
        found = problem(self, self.held(outcome))
        self.assertEqual((found['predicate'], found['fields']), ('changed_style_wrong', ['character']))
        self.assertEqual(found['character_items'], ['attr_shadecolor', 'attr_usekerning'])
        self.assertEqual((found['character_item_count'], found['size_expected'], found['size_found'], found['unrequested_changed']), (2, 11.0, 11.0, True))
        self.no_follow_on(owned, trace)

    def test_a_present_but_unfilled_default_sub_set_is_not_used(self) -> None:
        """Source09 held here. The default sub-set, filled or not, is not what the change writes: the read whole shape is."""
        hwp = rich_document(SAME_FONTS)
        hwp.direct_edit_char_shape = hwp.legacy_subsets_unfilled = True
        _owned, inner, _trace, _start, outcome = self.change(document=lambda: hwp)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(inner.styles[0]['char'], canonical_char({**SAME_FONTS, 'Height': 1100}))

    def test_a_shape_never_set_back_changes_nothing_and_holds_with_the_size_not_applied(self) -> None:
        """The set-back answers None without reaching the parent: the set keeps no CharShape, as C06 found its defaults."""
        owned, inner, trace, _start, outcome = self.change(Fault(f'call:{L}CreateSet().SetItem', returning(None), skip=1))
        found = problem(self, self.held(outcome))
        self.assertEqual(inner.styles[0]['char'], RICH)
        self.assertEqual((found['predicate'], found['fields'], found['character_items'], found['character_item_count']),
                         ('changed_style_wrong', ['character'], ['attr_height'], 1))
        self.assertEqual((found['size_expected'], found['size_found'], found['unrequested_changed']), (11.0, 10.0, False))
        self.no_follow_on(owned, trace)

    def test_a_create_from_the_rich_base_keeps_it_too(self) -> None:
        """The create's own filled path, unchanged: based on 바탕글, the new style is 바탕글's whole shape at 14 pt."""
        owned, inner, trace, start, outcome = self.run_with('create', lambda owned: create_request(owned, based_on='바탕글', size_pt=14), document=rich_document)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual((inner.styles[1]['name'], inner.styles[1]['char'], inner.styles[1]['next']), ('새 본문', {**RICH, 'Height': 1400}, 1))
        self.assertEqual([item for item in trace[start:] if 'CreateItemSet' in item], [])
        owned, inner, trace, _start, outcome = self.run_with('create', lambda owned: create_request(owned, based_on='바탕글', size_pt=14),
                                                             Fault(f'call:{L}CreateSet().Item', made_instead), document=rich_document)
        found = problem(self, self.held(outcome))
        self.assertEqual((found['predicate'], found['name'], found['fields']), ('new_style_wrong', '새 본문', ['character', 'fonts']))
        self.assertEqual((found['size_expected'], found['size_found'], found['unrequested_changed']), (14.0, 14.0, True))
        self.assertEqual(found['character_items'], RESET_ITEMS)
        self.no_follow_on(owned, trace)


class WithoutAWholeShape(_Legacy):
    """A read shape without every documented item: refused before anything is written, the caret given back. (Source09 refused
    defaults without a CharShape sub-set here; C06 found them so, and they are the positive now.)"""

    def assert_refused(self, owned, inner: StyleHwp, trace: list, start: int, outcome: dict, *, dirty_first: bool, caret) -> None:
        step = outcome['steps'][-1]
        self.assertEqual(step.get('refusal_code'), 'style_shape_unverified', step)
        self.assertIn('nothing was written', step.get('refusal_message', ''))
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
        self.assertEqual(len(executes(inner)), 1 if dirty_first else 0, 'nothing more is written')
        seen = trace[start:]
        after = seen[seen.index(f'call:{S}ItemExist') + 1:]
        for name in ('call:CreateAction', f'call:{S}SetItem', f'call:{L}CreateSet().CreateItemSet', f'call:{L}Execute', f'call:{L}CreateSet().SetItem'):
            self.assertNotIn(name, after, 'no action made, no item written, no set-back and no Execute')
        self.assertIn('call:set_pos', after, 'the caret is given back')
        self.assertIn('call:get_text_file', after, 'and the document read again')
        self.assertEqual(inner.cursor, caret)
        if dirty_first:
            self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')

    def test_a_read_shape_without_every_item_refuses_the_change_cleanly(self) -> None:
        def partial() -> StyleHwp:
            hwp = rich_document()
            hwp.char_shape_drop = ('BorderFill',)
            return hwp

        cases = (('BorderFill absent', partial, (), False, lambda owned: to_11(owned)),
                 ('ItemExist False', rich_document, (Fault(f'call:{S}ItemExist', returning(False)),), False, lambda owned: to_11(owned)),
                 # The earlier unsaved edit applies 개요 1 to the last paragraph of styles_document: 개요 1's one plain body use.
                 ('after a dirty edit', styles_document, (Fault(f'call:{S}ItemExist', returning(False)),), True,
                  lambda owned: update_request(owned, '개요 1', size_pt=13)))
        for name, document, faults, dirty_first, request_of in cases:
            with self.subTest(case=name):
                owned, inner, trace = self.owned(document, dirty_first=dirty_first)
                caret = inner.cursor
                request = request_of(owned)
                start = len(trace)
                object.__getattribute__(owned.hwp, '_t')[2].extend(faults)
                outcome = bundle(owned, **request)
                self.assert_refused(owned, inner, trace, start, outcome, dirty_first=dirty_first, caret=caret)


class FirstFaults(_Legacy):
    """Each fault of the change's read-set calls, its set-back and its action holds at once, with zero further SDK calls; never a refusal.
    Source09's faults at the filled sub-set's ItemExist, Item and SetItem are at the read set's now."""

    def held_at(self, fault: Fault, site: str | None, returned: str | None, *, mutated: bool = False) -> dict:
        owned, inner, trace, _start, outcome = self.run_with('update', to_11, fault, document=rich_document)
        self.assert_held(owned, inner, trace, outcome, mutated=mutated)
        diagnostic = self.diagnostic(outcome)
        if site is not None:
            self.assertEqual((diagnostic.get('native_site'), diagnostic.get('native_return')), (site, returned), outcome)
        dumped = json.dumps(outcome, ensure_ascii=False)
        for text in (SECRET, 'Foreign', 'Hostile', 'PlainSub'):
            self.assertNotIn(text, dumped)
        return outcome

    def test_item_exist_answers_exactly_a_bool(self) -> None:
        for value, shape in ((1, 'int'), (0, 'int'), ('True', 'str'), (None, 'none'), (Foreign(), 'object')):
            with self.subTest(value=shape):
                self.held_at(Fault(f'call:{S}ItemExist', returning(value)), 'char_shape.item_exist', shape)
        for exc in (RuntimeError(SECRET), TypeError(SECRET)):
            with self.subTest(raised=type(exc).__name__):
                self.held_at(Fault(f'call:{S}ItemExist', raising(exc)), 'char_shape.item_exist', 'raised')

    def test_item_answers_an_object_with_set_item(self) -> None:
        # The first Item read is Height, an int: 0 would be well typed and refused as not the style's, so it is not among these.
        for value, shape in ((1100.0, 'float'), ('', 'str'), (None, 'none'), (True, 'true'), ([], 'list')):
            with self.subTest(value=shape):
                self.held_at(Fault(f'call:{S}Item', returning(value)), 'char_shape.item', shape)
        for exc in (RuntimeError(SECRET), TypeError(SECRET)):
            with self.subTest(raised=type(exc).__name__):
                self.held_at(Fault(f'call:{S}Item', raising(exc)), 'char_shape.item', 'raised')
        with self.subTest(value='no SetItem'):
            self.held_at(Fault('get:CharShape', returning(PlainSub())), 'char_shape.member', 'absent')

    def test_the_getters_entered_hold(self) -> None:
        for when in (f'get:{S}ItemExist', f'get:{S}Item', f'get:{S}SetItem'):
            for name, effect in (('raises', raising(RuntimeError(SECRET))), ('absent', raising(AttributeError(when))), ('none', returning(None)),
                                 ('plain', returning(1))):
                with self.subTest(when=when, effect=name):
                    self.held_at(Fault(when, effect), None, None)

    def test_no_check_calls_into_the_answer(self) -> None:
        # A foreign object as the read set: its ItemExist is looked up on it, without a hook, and is absent.
        for when, site, returned in ((f'call:{S}ItemExist', 'char_shape.item_exist', 'object'), (f'call:{S}Item', 'char_shape.item', 'object'),
                                     ('get:CharShape', 'char_shape.member', 'absent')):
            with self.subTest(when=when):
                TOUCHED.clear()
                self.held_at(Fault(when, returning(Hostile())), site, returned)
                self.assertEqual(TOUCHED, [])

    def test_the_sub_set_write_and_the_set_back_are_void(self) -> None:
        for value, shape in ((True, 'true'), (False, 'false'), (0, 'int'), (Foreign(), 'object')):
            with self.subTest(write='Height', value=shape):
                self.held_at(Fault(f'call:{S}SetItem', returning(value)), 'subset_set_item.Height', shape)
            with self.subTest(write='set-back', value=shape):
                self.held_at(Fault(f'call:{L}CreateSet().SetItem', returning(value), skip=1), 'set_item.CharShape', shape)
        for when, skip, site in ((f'call:{S}SetItem', 0, 'subset_set_item.Height'), (f'call:{L}CreateSet().SetItem', 1, 'set_item.CharShape')):
            with self.subTest(raised=site):
                self.held_at(Fault(when, raising(TypeError(SECRET)), skip=skip), site, 'raised')

    def test_execute_answers_exactly_true(self) -> None:
        for name, effect in (('false', returning(False)), ('none', returning(None)), ('raises', raising(RuntimeError(SECRET)))):
            with self.subTest(answer=name):
                outcome = self.held_at(Fault(f'call:{L}Execute', effect), None, None, mutated=True)
                self.assertNotIn('style_proof', outcome['steps'][-1], 'nothing was read after it')

    def test_after_an_earlier_dirty_edit_each_first_fault_keeps_the_debt(self) -> None:
        """The earlier edit applies 개요 1 to the last paragraph of styles_document, which gives 개요 1 one plain body use to change."""
        cases = ((Fault(f'call:{S}ItemExist', raising(RuntimeError(SECRET))), False),
                 (Fault(f'call:{S}Item', returning(None)), False),
                 (Fault(f'call:{S}SetItem', returning(True)), False),
                 (Fault(f'call:{L}CreateSet().SetItem', returning(True), skip=1), False),
                 (Fault(f'call:{L}Execute', returning(False)), True))
        for fault, mutated in cases:
            with self.subTest(when=fault.when):
                owned, inner, trace, _start, outcome = self.run_with('update', lambda owned: update_request(owned, '개요 1', size_pt=13), fault,
                                                                     document=styles_document, dirty_first=True)
                step = outcome['steps'][-1]
                self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, mutated), step)
                self.assertNotIn('refusal_code', step)
                self.assertEqual(after_fault(trace), [])
                self.assertEqual(len(executes(inner)), 1, "only the earlier edit reached the fake; the fault stood in for this one's action")
                self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')
                self.assertTrue(outcome['dirty'])

    def test_a_base_exception_holds_and_reaches_no_further_call(self) -> None:
        for when, exc, mutated in ((f'call:{S}Item', KeyboardInterrupt(SECRET), False),
                                   (f'call:{S}SetItem', SystemExit(SECRET), False),
                                   (f'call:{L}Execute', KeyboardInterrupt(SECRET), True)):
            with self.subTest(when=when, exc=type(exc).__name__):
                owned, inner, trace, _start, outcome = self.run_with('update', to_11, Fault(when, raising(exc)), document=rich_document)
                step = outcome['steps'][-1]
                self.assertFalse(outcome['ok'], outcome)
                self.assertNotIn('refusal_code', step)
                self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(after_fault(trace), [])
                self.assertEqual(step['mutation_may_have_persisted'], mutated, step)
                self.assertEqual(executes(inner), [], 'the fault stood in for the action itself')
                self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False))


class Facts(unittest.TestCase):
    """The character facts: measured from the model the proof read, bounded, and never in the way of the hold."""

    def test_an_old_style_s_drift_names_what_drifted(self) -> None:
        hwp = rich_document()
        hwp.after_execute = lambda owner: owner.styles[1]['char'].update(Italic=True, Height=1300)
        outcome = run_on(self, hwp)
        step = outcome['steps'][-1]
        problems = [item for item in step['style_proof']['problems'] if item['predicate'] == 'old_style_changed']
        self.assertEqual(len(problems), 1, step['style_proof'])
        found = problems[0]
        self.assertEqual((found['name'], found['fields'], found['character_items'], found['character_item_count']), ('본문', ['character'], ['attr_height', 'italic'], 2))
        self.assertEqual((found['size_expected'], found['size_found'], found['unrequested_changed']), (10.0, 13.0, True))

    def test_a_font_whose_typeinfo_drifted_is_named_by_language(self) -> None:
        hwp = rich_document()
        hwp.after_execute = lambda owner: owner.font_info.update({'함초롬바탕': {**TYPEINFO, 'Weight': '9'}})
        step = run_on(self, hwp)['steps'][-1]
        found = next(item for item in step['style_proof']['problems'] if item['predicate'] == 'old_style_changed' and item['name'] == '본문')
        self.assertEqual(found['character_items'], [f'font_content_{key}' for key in KEYS])
        self.assertEqual((found['size_expected'], found['size_found'], found['unrequested_changed']), (10.0, 10.0, True))

    def test_the_new_style_s_shape_names_what_differs(self) -> None:
        hwp = rich_document()
        hwp.after_execute = lambda owner: owner.styles[1]['char'].update(Italic=False)
        outcome = run_on(self, hwp, create=True)
        found = problem(self, outcome['steps'][-1])
        self.assertEqual((found['predicate'], found['fields'], found['character_items'], found['size_expected'], found['size_found'], found['unrequested_changed']),
                         ('new_style_wrong', ['character'], ['italic'], 14.0, 14.0, True))

    def test_a_requested_item_left_unchanged_is_not_unrequested(self) -> None:
        hwp = rich_document()
        hwp.after_execute = lambda owner: owner.styles[0]['char'].update(Bold=False)
        step = run_on(self, hwp, bold=True)['steps'][-1]
        found = next(item for item in step['style_proof']['problems'] if item['predicate'] == 'changed_style_wrong')
        self.assertEqual((found['character_items'], found['unrequested_changed'], found['size_expected'], found['size_found']), (['bold'], False, 11.0, 11.0))

    def test_the_items_are_bounded_and_counted_in_full(self) -> None:
        before = {'attrs': {f'a{number}': '0' for number in range(40)} | {'height': '1000', 'Not A Token': '0'}, 'fonts': {}, 'font_types': {},
                  'children': {}, 'bold': False, 'italic': False, 'underline': None}
        after = {**before, 'attrs': {key: '1' for key in before['attrs']} | {'height': '1000'}}
        facts = native_styles._character_facts(after, before, {}, None, None)  # noqa: SLF001
        self.assertEqual(len(facts['character_items']), native_styles.CHARACTER_ITEMS_SHOWN)
        self.assertEqual(facts['character_item_count'], 41, 'the label that is no token counted, not shown')
        self.assertTrue(all(style_rules._TOKEN.fullmatch(item) for item in facts['character_items']))  # noqa: SLF001
        self.assertEqual((facts['size_expected'], facts['size_found'], facts['unrequested_changed']), (10.0, 10.0, True))
        unreadable = native_styles._character_facts({**before, 'attrs': {'height': 'x'}}, {**before, 'attrs': {}}, {'size_pt': 11}, None, None)  # noqa: SLF001
        self.assertNotIn('size_expected', unreadable)
        self.assertNotIn('size_found', unreadable)

    def test_a_fault_while_measuring_them_keeps_the_hold_and_its_cause(self) -> None:
        with patch.object(native_styles, '_character_facts', side_effect=RuntimeError('PRIVATE')):
            outcome = run_on(self, unbound_document())
        step = outcome['steps'][-1]
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertNotIn('refusal_code', step)
        found = step['style_proof']['problems'][0]
        self.assertEqual((found['predicate'], found['fields']), ('changed_style_wrong', ['character']))
        for key in ('size_expected', 'size_found', 'character_items', 'character_item_count', 'unrequested_changed'):
            self.assertNotIn(key, found)
        self.assertNotIn('PRIVATE', json.dumps(outcome, ensure_ascii=False))

    def test_the_facts_are_checked_against_their_fixed_shape(self) -> None:
        facts = run_on(self, unbound_document())['steps'][-1]['style_proof']
        self.assertEqual(style_rules.style_proof_facts(facts), facts)
        item = facts['problems'][0]
        bad = ({'size_expected': '11'}, {'size_expected': True}, {'size_expected': -1.0}, {'size_expected': math.nan}, {'size_found': math.inf},
               {'size_found': 10.0 ** 9}, {'character_items': []}, {'character_items': ['PRIVATE TEXT']}, {'character_items': ['italic', 'italic']},
               {'character_items': 'italic'}, {'character_items': [f'a{n}' for n in range(native_styles.CHARACTER_ITEMS_SHOWN + 1)]},
               {'character_item_count': -1}, {'character_item_count': True}, {'character_item_count': 0}, {'unrequested_changed': 1},
               {'unrequested_changed': 'true'}, {'character_items': [{}]}, {'fields': [{}]}, {'fields': [['character']]})
        for change in bad:
            with self.subTest(change=str(change)[:60]):
                self.assertIsNone(style_rules.style_proof_facts({**facts, 'problems': [{**item, **change}]}), 'refused, and never by raising')


def run_on(test: unittest.TestCase, hwp: StyleHwp, *, create: bool = False, **fields) -> dict:
    """One change of 바탕글 to 11 pt (or one create based on 바탕글 at 14 pt) through the real owned service, without a trace."""
    owned = owned_doc(test, hwp, undo_stack=[], redo_stack=[])
    if create:
        listing = bundle(owned, op='style_inventory')['steps'][0]['result']
        rows = {item['name']: item for item in listing['styles']}
        request = {**CREATE, 'name': '새 본문', 'kind': 'paragraph', 'based_on': '바탕글', 'expected_based_on_hash': rows['바탕글']['hash'],
                   'expected_styles_hash': listing['styles_hash'], 'size_pt': 14}
    else:
        request = to_11(owned, **fields)
    return bundle(owned, **request)


class Reporting(unittest.TestCase):
    """The facts reach the caller through the CLI normaliser and the runtime journal."""

    def test_the_cli_keeps_the_facts(self) -> None:
        outcome = run_on(self, unbound_document())
        step = outcome['steps'][-1]
        self.assertIn('character_items', step['style_proof']['problems'][0])
        normalized = normalize_command_bundle(outcome)
        self.assertEqual(normalized['steps'][0]['extras']['style_proof'], step['style_proof'])

    def test_a_fault_while_projecting_the_facts_keeps_the_hold(self) -> None:
        with patch.object(style_rules, 'style_proof_facts', side_effect=RuntimeError('PRIVATE')):
            outcome = run_on(self, unbound_document())
        step = outcome['steps'][-1]
        self.assertEqual((outcome['ok'], step['mutation_may_have_persisted'], step.get('native_custody_held')), (False, True, True))
        self.assertNotIn('style_proof', step)
        self.assertTrue(step['outcome_detail'].endswith('nothing was rolled back'), step['outcome_detail'])
        self.assertNotIn('PRIVATE', json.dumps(outcome, ensure_ascii=False))


class Journal(unittest.TestCase):
    owned = recovery.RecoveryTests.owned

    def test_the_runtime_journal_keeps_the_facts_of_a_held_change(self) -> None:
        manager, session, _wrapper, _calls, wc = self.owned()
        hwp = unbound_document()
        hwp.IsModified = False
        session._active_handle.hwp = hwp  # Synthetic SDK adapter, not a native rebind.
        binding = {'session_id': 'recovery', 'source_filename': 'fixture.hwpx', 'command_generation': 0, 'native_command_sequence': 0}
        service = object.__new__(svc.LocalCliService)
        service.runtime_manager = manager
        service.command_packages = get_command_package_registry()
        service._require_ready_runtime = lambda *a: None
        service._load_active_binding = lambda **k: dict(binding)
        service._read_binding = lambda **k: dict(binding)
        service._save_binding = lambda b: b
        service._update_live_binding = lambda b, **k: b
        service._record_local_cli_command = lambda *a, **k: None
        service._working_copy_path = lambda b: wc
        service._verify_artifact_readback = lambda *a, **k: wc
        service._artifact_name = lambda **k: 'fixture.hwpx'
        service._artifact_download_path = lambda **k: 'fixture-download'
        service._bundle_page_evidence = lambda h, **kwargs: {'page': 1}
        service._bundle_compact_snapshot = lambda h: {}
        self.enterContext(patch.object(svc, 'snapshot_live_location', lambda **k: {}))
        found = service.command_bundle(steps=[{'op': 'style_inventory', 'name': '바탕글'}])['steps'][0]['result']['styles'][0]
        outcome = service.command_bundle(steps=[{'op': 'style_update_exact', 'confirm_layout': True, 'style_name': '바탕글', 'expected_style_hash': found['hash'],
                                                 'expected_uses': found['uses']['total'], 'expected_uses_hash': found['uses']['hash'], 'all_uses': True,
                                                 'size_pt': 11}])
        step = outcome['steps'][0]
        self.assertTrue(step.get('native_custody_held'), step)
        command = session._commands_by_id[session._last_command_id]
        self.assertTrue(command.completed_event.wait(1))
        journal = json.loads(rt.command_journal_path(session.session_root).read_text())
        kept = journal['commands'][-1]['result']['steps'][0]
        self.assertEqual(kept['style_proof'], step['style_proof'])
        self.assertEqual(kept['style_proof']['problems'][0]['character_items'], UNBOUND_ITEMS)
        decoded = json.dumps(journal, ensure_ascii=False)
        for name in TEXTS:
            self.assertNotIn(name, decoded)


if __name__ == '__main__':
    unittest.main()
