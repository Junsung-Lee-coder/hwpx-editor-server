"""Named styles: creating and changing a style through the legacy action family of Hancom's own examples.

- Create (forum.developer.hancom.com topic 2505 post 2, 2025): ``CreateAction("StyleAddNew")``, ``CreateSet()``,
  ``GetDefault(set)``, ``SetItem`` NameLocal, NameEng and Next, the CharShape sub-set (``ItemExist``, ``Item``) with the
  requested items, ``SetItem("CharShape", sub-set)``, ``Execute(set)``.
- Change (topic 1552 post 4, 2024; Hangul 2018 and later): the style's whole character shape read first as ``IHwpObject.CharShape``
  at its plain use, with the requested items written into it (tests.test_native_styles_snapshot); then
  ``CreateAction("StyleDirectEdit")``, ``CreateSet()``, ``GetDefault(set)``, ``SetItem("NameLocal", <the style to change>)``,
  ``SetItem("CharShape", <that set>)``, ``Execute(set)``. C06 found StyleDirectEdit's defaults without a CharShape sub-set. The example
  makes the sub-set with ``CreateItemSet``; Hancom's answer in topic 1044 post 6 says a sub-set made that way needs all its child
  items set.
The installed type library (HwpObject.tlb of Hancom 13.0.0.3457, sha256 047f64bd...) declares GetDefault VT_I4, Execute
VT_BOOL, SetItem VT_VOID, ItemExist VT_BOOL, Item VT_VARIANT and CreateAction, CreateSet and CreateItemSet VT_DISPATCH; the
manual (HwpAutomation_2504 pages 61-64) says void for GetDefault and Execute. GetDefault must answer exactly the int each
action answered natively (1 for both, GD01 and GD02), Execute exactly True, SetItem exactly None, before the next native
call. Sub-sets are copies under Automation (forum topic 2354 post 5): the CharShape sub-set is set back on the parent set
before Execute (tests.test_native_styles_copy).

Through the real bundle validation, package dispatch, owned service and engine, with every attribute, call and write on the
SDK object recorded (tests.test_native_styles_repairs). The SDK is tests.native_styles_fakes; not Hancom evidence.
"""
from __future__ import annotations

import copy
import json
import unittest

from tests import test_native_styles_entry as entry
from tests.native_format_fakes import char
from tests.test_native_styles import BODY, CREATE, UPDATE, executes, plain_document
from tests.native_styles_fakes import styles_document
from tests.test_native_styles_repairs import Fault, after_fault, bundle, raising, returning

L = entry.LEGACY
SECRET = 'C:/Users/secret/문서 본문 비밀'


class Foreign:
    """An answer of a foreign class: its label is the shape 'object', never its class name."""


class One(int):
    """An int subclass equal to 1: not the exact builtin int GetDefault answered natively."""


TOUCHED: list[str] = []


def _touch(name: str) -> None:
    TOUCHED.append(name)
    raise AssertionError(f'{name} of the answer was called')


class TouchyOne(int):
    """An int subclass equal to 1 whose comparison, hash, truth, conversions and text record the touch and raise: a check that
    compares before it looks at the exact type calls into it."""

    def __eq__(self, other): _touch('__eq__')  # noqa: E704
    def __ne__(self, other): _touch('__ne__')  # noqa: E704
    def __hash__(self): _touch('__hash__')  # noqa: E704
    def __bool__(self): _touch('__bool__')  # noqa: E704
    def __index__(self): _touch('__index__')  # noqa: E704
    def __int__(self): _touch('__int__')  # noqa: E704
    def __repr__(self): _touch('__repr__')  # noqa: E704
    def __str__(self): _touch('__str__')  # noqa: E704
    def __format__(self, spec): _touch('__format__')  # noqa: E704


class Hostile:
    """A foreign answer whose every hook the checks could reach records the touch and raises."""

    def __eq__(self, other): _touch('__eq__')  # noqa: E704
    def __ne__(self, other): _touch('__ne__')  # noqa: E704
    def __hash__(self): _touch('__hash__')  # noqa: E704
    def __bool__(self): _touch('__bool__')  # noqa: E704
    def __len__(self): _touch('__len__')  # noqa: E704
    def __index__(self): _touch('__index__')  # noqa: E704
    def __repr__(self): _touch('__repr__')  # noqa: E704
    def __str__(self): _touch('__str__')  # noqa: E704
    def __format__(self, spec): _touch('__format__')  # noqa: E704


class PlainSub:
    """A sub-set object with no SetItem at all: a plain object, no descriptor and no lookup hook."""


def create_request(owned, **fields) -> dict:
    listing = bundle(owned, op='style_inventory')['steps'][0]['result']
    rows = {item['name']: item for item in listing['styles']}
    base = fields.pop('based_on', '본문')
    return {**CREATE, 'name': '새 본문', 'kind': 'paragraph', 'based_on': base, 'expected_based_on_hash': rows[base]['hash'],
            'expected_styles_hash': listing['styles_hash'], **fields}


def update_request(owned, name: str = '본문', **fields) -> dict:
    found = bundle(owned, op='style_inventory', name=name)['steps'][0]['result']['styles'][0]
    return {**UPDATE, 'style_name': name, 'expected_style_hash': found['hash'], 'expected_uses': found['uses']['total'],
            'expected_uses_hash': found['uses']['hash'], 'all_uses': True, **fields}


class _Legacy(entry._Entry):
    def run_with(self, op: str, request_of, *faults: Fault, document=None, dirty_first: bool = False):
        owned, inner, trace = self.owned(document or entry.DOCUMENTS[op], dirty_first=dirty_first)
        request = request_of(owned)
        start = len(trace)
        object.__getattribute__(owned.hwp, '_t')[2].extend(faults)
        outcome = bundle(owned, **request)
        for fault in faults:
            self.assertTrue(fault.fired, f'the fault at {fault.when} was reached')
        return owned, inner, trace, start, outcome

    def diagnostic(self, outcome: dict) -> dict:
        return outcome['steps'][-1].get('failure_diagnostic', {})


class Family(_Legacy):
    def test_create_goes_through_style_add_new_and_never_through_haction(self) -> None:
        owned, inner, trace, start, outcome = self.run_with('create', lambda owned: create_request(owned, size_pt=14, bold=True))
        self.assertTrue(outcome['ok'], outcome)
        result = outcome['steps'][-1]['result']
        self.assertEqual(result['result']['native_actions'], ['Execute:StyleAddNew'])
        self.assertEqual(result['undo']['native_editing_actions'], 1)
        self.assertEqual([call[:2] for call in inner.style_calls()], [('GetDefault', 'StyleAddNew'), ('Execute', 'StyleAddNew')])
        seen = trace[start:]
        self.assertEqual([item for item in seen if item.startswith(('get:HAction', 'get:HParameterSet'))], [], 'no HAction or HSet family at all')
        order = [item for item in seen if item.startswith('call:') and item != 'call:get_text_file' and 'pos' not in item]
        self.assertEqual(order, ['call:CreateAction', f'call:{L}CreateSet', f'call:{L}GetDefault', f'call:{L}CreateSet().SetItem',
                                 f'call:{L}CreateSet().SetItem', f'call:{L}CreateSet().SetItem', f'call:{L}CreateSet().ItemExist',
                                 f'call:{L}CreateSet().Item', *[f'call:{L}CreateSet().Item().SetItem'] * 2, f'call:{L}CreateSet().SetItem',
                                 f'call:{L}Execute'])
        self.assertLess(seen.index(f'call:{L}CreateSet'), seen.index('call:set_pos'), 'the action and its set are made before the caret moves')
        self.assertLess(seen.index('call:set_pos'), seen.index(f'call:{L}GetDefault'), 'GetDefault reads the state at the base style')
        items = executes(inner)[0][2]
        # Next is the new style's own slot, right after the base 본문 (index 1): 2, an index of the list after the insertion.
        self.assertEqual({key: items[key] for key in ('NameLocal', 'NameEng', 'Next')}, {'NameLocal': '새 본문', 'NameEng': '새 본문', 'Next': 2})

    def test_change_goes_through_style_direct_edit_with_the_name_as_its_selector(self) -> None:
        owned, inner, trace, start, outcome = self.run_with('update', lambda owned: update_request(owned, size_pt=11))
        self.assertTrue(outcome['ok'], outcome)
        result = outcome['steps'][-1]['result']
        self.assertEqual(result['result']['native_actions'], ['Execute:StyleDirectEdit'])
        self.assertEqual([call[:2] for call in inner.style_calls()], [('GetDefault', 'StyleDirectEdit'), ('Execute', 'StyleDirectEdit')])
        seen = trace[start:]
        self.assertEqual([item for item in seen if item.startswith(('get:HAction', 'get:HParameterSet'))], [])
        order = [item for item in seen if item.startswith('call:') and item != 'call:get_text_file' and 'pos' not in item
                 and item not in ('call:CharShape.ItemExist', 'call:CharShape.Item')]
        self.assertEqual(order, ['call:CharShape.SetItem', 'call:CreateAction', f'call:{L}CreateSet', f'call:{L}GetDefault', f'call:{L}CreateSet().SetItem',
                                 f'call:{L}CreateSet().SetItem', f'call:{L}Execute'],
                         'the read whole shape takes the requested item, then the action is made and the shape set back on its set once, before Execute')
        items = executes(inner)[0][2]
        self.assertEqual(items['NameLocal'], '본문', 'NameLocal names the style to change; it is never a new name')
        self.assertEqual(items['NameEng'], 'Body', "the set's other items are the style's own, from its plain use at the caret")


class Returns(_Legacy):
    """Each return is exactly what the installed type library declares and the native witnesses observed, or the session is held at
    once with a fixed site label."""

    VOID_VIOLATIONS = ((True, 'true'), (False, 'false'), (0, 'int'), ('', 'str'), ([], 'list'), (object(), 'object'), (Foreign(), 'object'))
    GET_DEFAULT_VIOLATIONS = ((None, 'none'), (True, 'true'), (False, 'false'), (0, 'int'), (-1, 'int'), (2, 'int'), (2 ** 40, 'int'), ('1', 'str'),
                              (1.0, 'float'), (One(1), 'object'), (Foreign(), 'object'), ([1], 'list'))

    def held_at(self, op: str, request_of, fault: Fault, site: str, returned: str) -> dict:
        owned, inner, trace, _start, outcome = self.run_with(op, request_of, fault)
        self.assert_held(owned, inner, trace, outcome)
        self.assertEqual((self.diagnostic(outcome).get('native_site'), self.diagnostic(outcome).get('native_return')), (site, returned), outcome)
        return outcome

    def test_get_default_answers_exactly_the_int_observed_for_its_action(self) -> None:
        """Declared VT_I4; GD01 (StyleAddNew) and GD02 (StyleDirectEdit) each answered the exact builtin int 1. Nothing else passes,
        not True, not another int, not an int subclass; what 1 means is not established, so it is no success of its own."""
        for op, request_of in (('create', create_request), ('update', lambda owned: update_request(owned, size_pt=11))):
            with self.subTest(op=op, value='observed int 1'):
                _owned, inner, _trace, _start, outcome = self.run_with(op, request_of)
                self.assertTrue(outcome['ok'], outcome)
                self.assertEqual(inner.legacy_defaults_result, 1)
            for value, shape in self.GET_DEFAULT_VIOLATIONS:
                with self.subTest(op=op, value=repr(value)):
                    self.held_at(op, request_of, Fault(f'call:{L}GetDefault', returning(value)), 'get_default.call', shape)
            with self.subTest(op=op, value='raises'):
                outcome = self.held_at(op, request_of, Fault(f'call:{L}GetDefault', raising(RuntimeError(SECRET))), 'get_default.call', 'raised')
                self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False))

    def test_no_check_calls_into_the_answer(self) -> None:
        """GetDefault, the CharShape set-back and Execute: an int subclass equal to 1 and a foreign object are named 'object' by
        their exact type alone; none of their comparison, hash, truth, conversion or text hooks is called."""
        update, create = (lambda owned: update_request(owned, size_pt=11)), (lambda owned: create_request(owned, size_pt=14))
        for answer in (TouchyOne(1), Hostile()):
            name = type(answer).__name__
            for op, request_of, when, skip, site in (('create', create, f'call:{L}GetDefault', 0, 'get_default.call'),
                                                     ('update', update, f'call:{L}GetDefault', 0, 'get_default.call'),
                                                     ('create', create, f'call:{L}CreateSet().SetItem', 3, 'set_item.CharShape'),
                                                     ('update', update, f'call:{L}CreateSet().SetItem', 1, 'set_item.CharShape')):
                with self.subTest(answer=name, op=op, site=site):
                    TOUCHED.clear()
                    outcome = self.held_at(op, request_of, Fault(when, returning(answer), skip=skip), site, 'object')
                    self.assertEqual(TOUCHED, [])
                    self.assertNotIn(name, json.dumps(outcome, ensure_ascii=False))
            for op, request_of in (('create', create), ('update', update)):
                with self.subTest(answer=name, op=op, site='Execute'):
                    TOUCHED.clear()
                    owned, inner, trace = self.owned(entry.DOCUMENTS[op])
                    request = request_of(owned)
                    inner.execute_result = answer
                    outcome = bundle(owned, **request)
                    step = outcome['steps'][-1]
                    self.assertEqual((outcome['ok'], step.get('mutation_may_have_persisted')), (False, True), step)
                    self.assertIn('Execute answered object', step.get('outcome_detail', ''))
                    self.assertEqual(TOUCHED, [])
                    self.assertNotIn(name, json.dumps(outcome, ensure_ascii=False))

    def test_the_accepted_get_default_answers_are_the_observed_ones_per_action(self) -> None:
        from app import native_styles
        self.assertEqual(native_styles._GET_DEFAULT_OBSERVED, {'StyleAddNew': frozenset({1}), 'StyleDirectEdit': frozenset({1})})  # noqa: SLF001

    def test_set_item_is_void(self) -> None:
        cases = (('create', 0, 'set_item.NameLocal'), ('create', 1, 'set_item.NameEng'), ('create', 2, 'set_item.Next'), ('create', 3, 'set_item.CharShape'),
                 ('update', 0, 'set_item.NameLocal'), ('update', 1, 'set_item.CharShape'))
        for op, skip, site in cases:
            request_of = (lambda owned: create_request(owned, size_pt=14)) if op == 'create' else (lambda owned: update_request(owned, size_pt=11))
            for value, shape in self.VOID_VIOLATIONS[:3]:
                with self.subTest(op=op, site=site, value=shape):
                    self.held_at(op, request_of, Fault(f'call:{L}CreateSet().SetItem', returning(value), skip=skip), site, shape)

    def test_a_sub_set_write_is_void(self) -> None:
        for op, when, request_of in (('create', f'call:{L}CreateSet().Item().SetItem', lambda owned: create_request(owned, size_pt=14)),
                                     ('update', 'call:CharShape.SetItem', lambda owned: update_request(owned, size_pt=11))):
            with self.subTest(op=op):
                self.held_at(op, request_of, Fault(when, returning(True)), 'subset_set_item.Height', 'true')

    def test_item_exist_is_a_bool_and_the_sub_set_an_object(self) -> None:
        # The change asks the read whole shape, not its defaults; its first Item is Height, whose well-typed 0 is not the style's.
        reads = (('create', lambda owned: create_request(owned, bold=True), f'call:{L}CreateSet().', 'item_exist.CharShape', 'item.CharShape',
                  ((0, 'int'), ('', 'str'), (None, 'none'))),
                 ('update', lambda owned: update_request(owned, size_pt=11), 'call:CharShape.', 'char_shape.item_exist', 'char_shape.item',
                  ((1100.0, 'float'), ('', 'str'), (None, 'none'))))
        for op, request_of, prefix, exist_site, item_site, item_values in reads:
            for value, shape in ((1, 'int'), ('True', 'str'), (None, 'none')):
                with self.subTest(op=op, read='ItemExist', value=shape):
                    self.held_at(op, request_of, Fault(f'{prefix}ItemExist', returning(value)), exist_site, shape)
            for value, shape in item_values:
                with self.subTest(op=op, read='Item', value=shape):
                    self.held_at(op, request_of, Fault(f'{prefix}Item', returning(value)), item_site, shape)

    def test_a_sub_set_without_set_item_holds_rather_than_refusing(self) -> None:
        """After the edit entered native navigation even a purely absent method is no clean refusal."""
        self.held_at('create', lambda owned: create_request(owned, size_pt=14), Fault(f'call:{L}CreateSet().Item', returning(PlainSub())), 'subset.member', 'absent')
        self.held_at('update', lambda owned: update_request(owned, size_pt=11), Fault('get:CharShape', returning(PlainSub())), 'char_shape.member', 'absent')

    def test_a_raising_legacy_call_holds_with_its_site_and_no_message(self) -> None:
        cases = (('create', 'call:CreateAction', 'create_action.call'), ('create', f'call:{L}CreateSet', 'create_set.call'),
                 ('create', f'call:{L}GetDefault', 'get_default.call'), ('create', f'call:{L}CreateSet().ItemExist', 'item_exist.CharShape'),
                 ('update', 'call:CharShape.ItemExist', 'char_shape.item_exist'), ('update', 'call:CharShape.Item', 'char_shape.item'))
        for op, when, site in cases:
            request_of = (lambda owned: create_request(owned, bold=True)) if op == 'create' else (lambda owned: update_request(owned, size_pt=11))
            with self.subTest(op=op, when=when):
                outcome = self.held_at(op, request_of, Fault(when, raising(RuntimeError(SECRET))), site, 'raised')
                self.assertNotIn(SECRET, json.dumps(outcome, ensure_ascii=False), 'no exception text reaches the reply')

    def test_execute_answers_exactly_true(self) -> None:
        """Declared VT_BOOL, and the True the same IDHwpAction.Execute answered natively for this repository's legacy commands. Any
        other answer comes after the action: an unknown outcome with a fixed shape, never a class name."""
        for op, request_of in (('create', lambda owned: create_request(owned, size_pt=14)), ('update', lambda owned: update_request(owned, size_pt=11))):
            with self.subTest(op=op, value='true'):
                _owned, _inner, _trace, _start, outcome = self.run_with(op, request_of)
                self.assertTrue(outcome['ok'], outcome)
            with self.subTest(op=op, value='none after the change'):
                owned, inner, trace = self.owned(entry.DOCUMENTS[op])
                request = request_of(owned)
                real = inner.legacy_execute
                inner.legacy_execute = lambda name, pset: (real(name, pset), None)[1]
                step = bundle(owned, **request)['steps'][-1]
                self.assertTrue(step.get('mutation_may_have_persisted'), f'None is not the declared BOOL: an unknown outcome after the action: {step}')
                self.assertIn('none', step.get('outcome_detail', ''))
            for value, shape in ((False, 'false'), (1, 'int'), ('True', 'str'), ([True], 'list'), (Foreign(), 'object')):
                with self.subTest(op=op, value=repr(value)):
                    owned, inner, trace = self.owned(entry.DOCUMENTS[op])
                    request = request_of(owned)
                    inner.execute_result = value
                    outcome = bundle(owned, **request)
                    step = outcome['steps'][-1]
                    self.assertFalse(outcome['ok'])
                    self.assertTrue(step.get('mutation_may_have_persisted'), f'Execute was issued: an unknown outcome, never rolled back: {step}')
                    self.assertNotIn('refusal_code', step)
                    self.assertIn(f'Execute answered {shape}', step.get('outcome_detail', ''))
                    self.assertNotIn('Foreign', json.dumps(outcome, ensure_ascii=False), 'no class name of the answer')


class Persisted(_Legacy):
    def test_a_new_style_is_the_whole_base_with_the_requested_changes(self) -> None:
        owned, inner, _trace, _start, outcome = self.run_with('create', lambda owned: create_request(owned, size_pt=14, bold=True, next_style='개요 1'))
        self.assertTrue(outcome['ok'], outcome)
        new = inner.styles[2]
        # 개요 1, index 2 before, is index 3 once the new style is in slot 2.
        self.assertEqual((new['name'], new['eng'], new['type'], new['next'], new['lock']), ('새 본문', '새 본문', 'Para', 3, 0))
        self.assertEqual(inner.styles[new['next']]['name'], '개요 1')
        self.assertEqual(new['char'], {**BODY['char'], 'Height': 1400, 'Bold': True})
        self.assertEqual(new['para'], BODY['para'], "the base's paragraph shape, untouched")
        result = outcome['steps'][-1]['result']
        self.assertEqual((result['style']['index'], result['style']['character']['size_pt'], result['based_on']['name']), (2, 14.0, '본문'))

    def test_a_new_style_without_properties_touches_no_sub_set(self) -> None:
        owned, inner, trace, start, outcome = self.run_with('create', lambda owned: create_request(owned))
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual([item for item in trace[start:] if 'ItemExist' in item or 'Item()' in item or '.Item' in item], [])
        self.assertEqual((inner.styles[2]['name'], inner.styles[2]['char'], inner.styles[2]['para'], inner.styles[2]['next']), ('새 본문', BODY['char'], BODY['para'], 2))

    def test_a_change_reaches_the_style_and_every_use_and_nothing_else(self) -> None:
        hwp = plain_document()
        others = copy.deepcopy([item for item in hwp.styles if item['name'] != '본문'])
        owned, inner, _trace, _start, outcome = self.run_with('update', lambda owned: update_request(owned, size_pt=11, bold=True), document=lambda: hwp)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(inner.styles[1]['char'], {**BODY['char'], 'Height': 1100, 'Bold': True})
        self.assertEqual((inner.styles[1]['para'], inner.styles[1]['name'], inner.styles[1]['eng'], inner.styles[1]['next']), (BODY['para'], '본문', 'Body', 1))
        self.assertEqual([item for item in inner.styles if item['name'] != '본문'], others)
        uses = [item for item in inner.every_paragraph() if item.get('style', 0) == 1]
        self.assertEqual(len(uses), 4)
        self.assertTrue(all(atom['cs'] == inner.styles[1]['char'] for item in uses for atom in item['atoms']))

    def test_a_change_from_another_style_s_defaults_is_an_unknown_outcome(self) -> None:
        """Were the set's other items another style's, StyleDirectEdit would write them over this one: the proof holds it."""
        hwp = plain_document()
        hwp.after_execute = lambda owner: owner.styles[1].update(eng='Normal')
        _owned, _inner, _trace, _start, outcome = self.run_with('update', lambda owned: update_request(owned, size_pt=11), document=lambda: hwp)
        step = outcome['steps'][-1]
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertIn('not as requested', step.get('outcome_detail', ''))


class Variants(_Legacy):
    """Variants no documented mechanism proves are refused before anything is written, with their own codes."""

    def refused_before_any_write(self, op: str, request_of, code: str, *, dirty_first: bool = False, native: bool = False) -> dict:
        # The earlier unsaved edit applies 개요 1 to the last paragraph of styles_document; the refusals below come before any use check.
        document = styles_document if dirty_first else None
        owned, inner, trace, start, outcome = self.run_with(op, request_of, document=document, dirty_first=dirty_first)
        step = outcome['steps'][-1]
        self.assertEqual(step.get('refusal_code'), code, step)
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
        self.assertEqual(len(executes(inner)), 1 if dirty_first else 0, 'nothing more is written')
        if dirty_first:
            self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')
        if not native:
            self.assertEqual([item for item in trace[start:] if 'CreateAction' in item or item.startswith(('call:set_pos', 'get:HAction'))], [],
                             'refused from the document text alone')
        return step

    def test_unproven_variants_are_refused_with_their_own_codes(self) -> None:
        cases = (('create', lambda owned: create_request(owned, kind='character', based_on='바탕글', bold=True), 'style_kind_unsupported'),
                 ('create', lambda owned: create_request(owned, align='center'), 'style_variant_unsupported'),
                 ('create', lambda owned: create_request(owned, size_pt=14, line_spacing_type='percent', line_spacing_value=180), 'style_variant_unsupported'),
                 ('update', lambda owned: update_request(owned, new_name='본문 새이름'), 'style_rename_unsupported'),
                 ('update', lambda owned: update_request(owned, new_english_name='Body Text', size_pt=11), 'style_rename_unsupported'),
                 ('update', lambda owned: update_request(owned, size_pt=11, align='center'), 'style_variant_unsupported'))
        for op, request_of, code in cases:
            for dirty_first in (False, True):
                with self.subTest(op=op, code=code, dirty_first=dirty_first):
                    step = self.refused_before_any_write(op, request_of, code, dirty_first=dirty_first)
                    self.assertTrue(step.get('refusal_message'))

    def test_defaults_without_a_character_shape_refuse_a_character_change_cleanly(self) -> None:
        """ItemExist answers a well-typed False: for a create, the character shape to change is not in its defaults; for a change, an item
        is not in the whole shape read at the style's use. Nothing is written."""
        create = lambda owned: create_request(owned, size_pt=14)  # noqa: E731
        # The earlier unsaved edit applies 개요 1 to the last paragraph of styles_document: 개요 1's one plain body use to change.
        cases = (('create', create, None, False), ('create', create, None, True), ('update', lambda owned: update_request(owned, size_pt=11), None, False),
                 ('update', lambda owned: update_request(owned, '개요 1', size_pt=13), styles_document, True))
        for op, request_of, document, dirty_first in cases:
            with self.subTest(op=op, dirty_first=dirty_first):
                fault = Fault(f'call:{L}CreateSet().ItemExist' if op == 'create' else 'call:CharShape.ItemExist', returning(False))
                owned, inner, trace, start, outcome = self.run_with(op, request_of, fault, document=document, dirty_first=dirty_first)
                step = outcome['steps'][-1]
                self.assertEqual(step.get('refusal_code'), 'style_variant_unsupported' if op == 'create' else 'style_shape_unverified', step)
                self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(len(executes(inner)), 1 if dirty_first else 0)
                after = after_fault(trace)
                self.assertNotIn(f'call:{L}Execute', after)
                self.assertNotIn(f'call:{L}CreateSet().CreateItemSet', after, 'no empty sub-set stands in for the base')
                self.assertIn('call:set_pos', after, 'the caret is given back')
                self.assertIn('call:get_text_file', after, 'and the document read again')

    def test_defaults_without_sub_sets_still_create_a_style_without_properties(self) -> None:
        hwp = styles_document()
        hwp.legacy_subsets = False
        _owned, inner, _trace, _start, outcome = self.run_with('create', create_request, document=lambda: hwp)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual((inner.styles[2]['name'], inner.styles[2]['char']), ('새 본문', BODY['char']))

    def test_a_paragraph_style_create_with_only_character_properties_is_eligible(self) -> None:
        owned, inner, _trace, _start, outcome = self.run_with('create', lambda owned: create_request(owned, based_on='바탕글', size_pt=11))
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual((inner.styles[1]['name'], inner.styles[1]['char']), ('새 본문', char(Height=1100)), 'in slot 1, right after 바탕글')


if __name__ == '__main__':
    unittest.main()
