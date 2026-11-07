"""Named styles: the font content, next style, run setting and native boundary rules.

- **Fonts:** a referenced font's whole content is kept, not only its name and type.
- **Next style:** it is a paragraph style.
- **Run settings:** a character keeps the run settings it had.
- **Native boundary:** a technical fault or malformed value after an edit has entered native navigation holds the session
  at once, with no further native call; a well-typed False or mismatch stays a clean refusal. Apply goes through HAction;
  create and change go through the legacy action family (CreateAction, CreateSet), whose GetDefault and SetItem are void.

Through the real bundle validation, package dispatch, service and engine. The SDK is tests.native_styles_fakes; the
fault matrix runs it through a real owned service and records every attribute, call and write the product makes on it.
Not Hancom evidence.
"""
from __future__ import annotations

import copy
import unittest
from typing import Any, Callable
from unittest.mock import patch

from app.native_numbering import NativeNumberingRefusal
from tests.native_format_fakes import char, paragraph, shape, table
from tests.native_styles_fakes import (StyleHwp, _LegacySet, _LegacyStyleAction, _StyleHAction, _StyleParams, _Typed, area, default_styles, style,
                                       styled, styles_document)
from tests.test_native_styles import APPLY, BODY, CREATE, UPDATE, executes, held, inventory, ok, plain_document, refused, row, run
from tests.test_native_styles_before import RecordedHwp, captured, listed
from tests.test_repair_runtime_safety import owned_doc

DOTUM = '함초롬돋움'
TYPEINFO = {'ArmStyle': '1', 'Contrast': '0', 'FamilyType': '2', 'Letterform': '1', 'Midline': '1', 'Proportion': '4', 'StrokeVariation': '1',
            'Weight': '6', 'XHeight': '1'}  # As in tests/fixtures/native_fields/before.hwpml2x.


def font_document(*, extra_use: bool = True) -> StyleHwp:
    """본문 used plainly twice; 함초롬돋움 used by one paragraph's direct format only, and by no style definition."""
    body = [paragraph('문서 제목'), styled('본문 첫 문단', 1, para_shape=BODY['para']), styled('본문 둘째 문단', 1, para_shape=BODY['para']),
            paragraph('앞 ', ('돋움 글자', char(Font=DOTUM)), ' 뒤'), paragraph('마지막 문단')]
    hwp = StyleHwp(body, areas=[area('header', paragraph('머리 문구'))])
    hwp.font_info = {DOTUM: dict(TYPEINFO), '함초롬바탕': dict(TYPEINFO)}
    return hwp


def retype(owner: StyleHwp, name: str = DOTUM) -> None:
    """Same Name and Type, another TYPEINFO: the font's content changed."""
    owner.font_info[name] = {**TYPEINFO, 'Weight': '8'}


def create(hwp: StyleHwp, **fields) -> dict:
    listing = inventory(hwp)
    based = next(item for item in listing['styles'] if item['name'] == fields.get('based_on', '본문'))
    return run(hwp, **CREATE, **{'based_on': '본문', 'expected_based_on_hash': based['hash'], 'expected_styles_hash': listing['styles_hash'], **fields})


def update(hwp: StyleHwp, name: str = '본문', **fields) -> dict:
    found = row(hwp, name)
    return run(hwp, **UPDATE, style_name=name, expected_style_hash=found['hash'], expected_uses=found['uses']['total'],
               expected_uses_hash=found['uses']['hash'], all_uses=True, **fields)


class FontContent(unittest.TestCase):
    """A font's TYPEINFO is part of its content: same Name and Type with other content is a changed font."""

    def test_create_and_update_hold_a_changed_font_that_only_direct_text_uses(self) -> None:
        for name, command in (('create', lambda hwp: create(hwp, name='새 본문', kind='paragraph', bold=True)),
                              ('update', lambda hwp: update(hwp, size_pt=11))):
            with self.subTest(command=name):
                hwp = font_document()
                hwp.after_execute = retype
                step = held(self, command(hwp))
                self.assertEqual(len(executes(hwp)), 1, 'the action ran, so the proof itself caught the change')
                self.assertIn('not as requested', step['outcome_detail'])

    def test_update_holds_a_changed_font_of_the_style_itself_that_was_not_requested(self) -> None:
        """본문 and its uses are the only users of 본문체: only the style's own font check sees its content change."""
        styles = default_styles()
        styles[1] = style('본문', eng='Body', chars=char(Font='본문체'), para=BODY['para'], next_index=1)
        uses = [styled(((text, char(Font='본문체')),), 1, para_shape=BODY['para']) for text in ('본문 첫 문단', '본문 둘째 문단')]
        hwp = StyleHwp([paragraph('문서 제목'), *uses, paragraph('마지막 문단')], styles=styles)
        hwp.font_info = {'본문체': dict(TYPEINFO)}
        hwp.after_execute = lambda owner: retype(owner, '본문체')
        held(self, update(hwp, size_pt=11))

    def test_apply_already_holds_a_changed_font(self) -> None:
        hwp = font_document()
        bound = row(hwp, '개요 1')['hash']
        hwp.after_execute = retype
        held(self, run(hwp, **APPLY, style_name='개요 1', expected_style_hash=bound, paragraph_index=4, expected_text='마지막 문단'))

    def test_a_requested_font_an_allocated_font_and_renumbered_fonts_still_pass(self) -> None:
        hwp = font_document()
        ok(self, create(hwp, name='돋움 본문', kind='paragraph', font_family=DOTUM))
        self.assertEqual((hwp.styles[2]['name'], hwp.styles[2]['char']['Font']), ('돋움 본문', DOTUM))
        hwp = font_document()
        ok(self, update(hwp, font_family=DOTUM, size_pt=11))
        self.assertEqual(hwp.styles[1]['char'], char(Font=DOTUM, Height=1100))
        for first in ([], ['나눔명조']):
            with self.subTest(renumbered=bool(first)), patch('app.native_format.installed_font_families', return_value={'나눔명조'}):
                hwp = font_document()
                hwp.fonts_first = first
                ok(self, create(hwp, name='명조 본문', kind='paragraph', font_family='나눔명조'))
                self.assertEqual((hwp.styles[2]['name'], hwp.styles[2]['char']['Font']), ('명조 본문', '나눔명조'))
        hwp = font_document()
        hwp.after_execute = lambda owner: setattr(owner, 'fonts_first', [DOTUM, '함초롬바탕'][::-1])
        ok(self, update(hwp, size_pt=11))


class NextStyleKind(unittest.TestCase):
    def test_a_character_next_style_is_refused_before_any_native_call(self) -> None:
        for name in ('쪽 번호', '강조'):
            with self.subTest(next_style=name):
                hwp = styles_document()
                hwp.cursor = (0, 0, 1)
                listing = inventory(hwp)
                based = next(item for item in listing['styles'] if item['name'] == '본문')
                hwp.calls.clear()
                outcome = run(hwp, **CREATE, name='소제목', kind='paragraph', based_on='본문', expected_based_on_hash=based['hash'],
                              expected_styles_hash=listing['styles_hash'], next_style=name)
                step = refused(self, outcome, 'style_kind_unsupported', hwp, ((0, 0, 1), None))
                self.assertIn('next_style', step['refusal_message'])
                self.assertEqual(hwp.style_calls(), [])
                self.assertEqual([call for call in hwp.calls if call[0] in {'set_pos', 'select_text'}], [], 'no navigation either')
                self.assertEqual(len(hwp.styles), 5)

    def test_a_paragraph_next_style_and_the_default_still_pass(self) -> None:
        # The new style in slot 2, right after the base 본문: 개요 1 (index 2 before) is 3 after it; the default names slot 2, itself.
        for fields, expected, named in (({'next_style': '개요 1'}, 3, '개요 1'), ({}, 2, '소제목')):
            with self.subTest(fields=fields):
                hwp = styles_document()
                ok(self, create(hwp, name='소제목', kind='paragraph', **fields))
                self.assertEqual((hwp.styles[2]['name'], hwp.styles[2]['next'], hwp.styles[hwp.styles[2]['next']]['name']), ('소제목', expected, named))

    def test_on_actual_text_a_built_in_character_style_as_next_style_stops_before_the_caret_moves(self) -> None:
        hwp = RecordedHwp(captured('native_fields/before.hwpml2x'))
        result = listed(hwp)
        based = result['styles'][0]
        outcome = run(hwp, op='style_create_exact', name='보고서 본문', kind='paragraph', based_on='바탕글', expected_based_on_hash=based['hash'],
                      expected_styles_hash=result['styles_hash'], next_style='줄 번호', confirm_layout=True)
        self.assertEqual(outcome['steps'][0].get('refusal_code'), 'style_kind_unsupported')
        self.assertEqual([call for call in hwp.calls if call[0] in {'set_pos', 'select_text', 'GetDefault', 'Execute'}], [])


def runs_document() -> StyleHwp:
    """A paragraph whose characters carry a synthetic run setting besides CharShape: 가나 'a', 다라 'b', 마바 'a'."""
    letters = [{'c': c, 'cs': char(), 'run': {'SyntheticRun': 'b' if number in (2, 3) else 'a'}} for number, c in enumerate('가나다라마바')]
    uniform = [{'c': c, 'cs': char(), 'run': {'SyntheticRun': 'u'}} for c in '하나의 설정 문단']
    return StyleHwp([paragraph('문서 제목'), paragraph(*letters), paragraph(*uniform), paragraph('마지막 문단')])


def swap_runs(owner: StyleHwp) -> None:
    """가나 'b', 다라 'a': the same settings in the paragraph and the same last run, held by other characters."""
    atoms = [atom for atom in owner.lists[0][1]['atoms'] if 'c' in atom]
    for number, atom in enumerate(atoms):
        atom['run'] = {'SyntheticRun': 'b' if number in (0, 1) else 'a'}


class RunOwnership(unittest.TestCase):
    """Run settings are compared per character, so a run split or merge passes and a retargeting does not."""

    def test_a_run_split_with_the_settings_kept_per_character_passes(self) -> None:
        for match in ('다라', '설정'):
            with self.subTest(match=match):
                hwp = runs_document()
                ok(self, run(hwp, **APPLY, style_name='강조', expected_style_hash=row(hwp, '강조')['hash'], match=match))

    def test_a_setting_moved_to_other_characters_is_an_unknown_outcome(self) -> None:
        hwp = runs_document()
        bound = row(hwp, '강조')['hash']
        hwp.after_execute = swap_runs
        step = held(self, run(hwp, **APPLY, style_name='강조', expected_style_hash=bound, match='설정'))
        self.assertIn('paragraph 1', step['outcome_detail'])


# The native boundary: a real owned service, its SDK object seen through one trace.

class Fault:
    """One fault at one event: ``when`` names the event (``call:get_pos``, ``set:HParameterSet.HStyle.Apply``), ``after`` an event that must
    have happened first, ``skip`` how many matching events pass first, ``count`` how many in a row it answers. ``effect`` raises, or returns
    the value the call returns."""

    def __init__(self, when: str, effect: Callable[[Callable[[], Any]], Any], *, after: str | None = None, skip: int = 0, count: int = 1) -> None:
        self.when, self.effect, self.after, self.skip, self.count = when, effect, after, skip, count
        self.armed = after is None
        self.fired = False
        self.left = count


def raising(exc: BaseException) -> Callable[[Callable[[], Any]], Any]:
    def effect(_real: Callable[[], Any]) -> Any:
        raise exc
    return effect


def returning(value: Any) -> Callable[[Callable[[], Any]], Any]:
    return lambda _real: value


class Traced:
    """The fake SDK object as the product sees it, recording every attribute it reads, call it makes and value it writes.

    An object a call returns (a legacy action from CreateAction, a set from CreateSet, a sub-set from Item or CreateItemSet) is
    traced too, under the call's key with ``().``; an argument the product passes back (a set to GetDefault or Execute, a sub-set
    to SetItem) reaches the fake as the fake's own object, as Hancom receives its own dispatch."""

    _NESTED = (_StyleHAction, _StyleParams, _Typed, _LegacyStyleAction, _LegacySet)

    def __init__(self, inner: Any, trace: list, faults: list[Fault], prefix: str = '') -> None:
        object.__setattr__(self, '_t', (inner, trace, faults, prefix))

    def _event(self, key: str, real: Callable[[], Any]) -> Any:
        _inner, trace, faults, _prefix = object.__getattribute__(self, '_t')
        trace.append(key)
        for fault in faults:
            if fault.after is not None and not fault.armed and fault.after in trace[:-1]:
                fault.armed = True
            if fault.armed and fault.left and fault.when == key:
                if fault.skip:
                    fault.skip -= 1
                    continue
                fault.fired = True
                fault.left -= 1
                trace.append('FAULT:' + key)
                return fault.effect(real)
        return real()

    def __getattr__(self, name: str) -> Any:
        inner, trace, faults, prefix = object.__getattribute__(self, '_t')
        key = prefix + name
        value = self._event('get:' + key, lambda: getattr(inner, name))
        if name == 'HSet':
            return value  # The set itself, as the native HSet property hands it to HAction.
        if isinstance(value, self._NESTED):
            return Traced(value, trace, faults, key + '.')
        if callable(value) and not isinstance(value, type):
            def call(*args: Any, **kwargs: Any) -> Any:
                raw = [arg.inner if isinstance(arg, Traced) else arg for arg in args]
                result = self._event('call:' + key, lambda: value(*raw, **kwargs))
                return Traced(result, trace, faults, key + '().') if isinstance(result, self._NESTED) else result
            return call
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        inner, _trace, _faults, prefix = object.__getattribute__(self, '_t')
        self._event('set:' + prefix + name, lambda: setattr(inner, name, value))

    @property
    def inner(self) -> Any:
        return object.__getattribute__(self, '_t')[0]


def after_fault(trace: list) -> list:
    marks = [number for number, item in enumerate(trace) if item.startswith('FAULT:')]
    assert len(marks) == 1, trace[-20:]
    return trace[marks[0] + 1:]


def bundle(owned, **fields) -> dict:
    with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
        return owned.service.command_bundle(steps=[{'label': 'x', **fields}])


class Boundary(unittest.TestCase):
    def owned(self, document: Callable[[], StyleHwp], *faults: Fault, dirty_first: bool = False) -> tuple[Any, StyleHwp, list]:
        trace: list = []
        inner = document()
        proxy = Traced(inner, trace, list(faults))
        owned = owned_doc(self, proxy, undo_stack=[], redo_stack=[])
        if dirty_first:
            found = bundle(owned, op='style_inventory', name='개요 1')['steps'][0]['result']['styles'][0]
            earlier = bundle(owned, **APPLY, style_name='개요 1', expected_style_hash=found['hash'], paragraph_index=12, expected_text='마지막 문단')
            self.assertTrue(earlier['ok'] and earlier['dirty'], earlier)
        return owned, inner, trace

    def request(self, owned, op: str) -> dict:
        if op == 'apply':
            found = bundle(owned, op='style_inventory', name='개요 1')['steps'][0]['result']['styles'][0]
            return {**APPLY, 'style_name': '개요 1', 'expected_style_hash': found['hash'], 'paragraph_index': 3, 'expected_text': '바탕 둘째 문단'}
        listing = bundle(owned, op='style_inventory')['steps'][0]['result']
        rows = {item['name']: item for item in listing['styles']}
        if op == 'create':
            return {**CREATE, 'name': '새 본문', 'kind': 'paragraph', 'based_on': '본문', 'expected_based_on_hash': rows['본문']['hash'],
                    'expected_styles_hash': listing['styles_hash'], 'bold': True}
        found = rows['본문']
        return {**UPDATE, 'style_name': '본문', 'expected_style_hash': found['hash'], 'expected_uses': found['uses']['total'],
                'expected_uses_hash': found['uses']['hash'], 'all_uses': True, 'size_pt': 11}

    def assert_held(self, owned, inner: StyleHwp, trace: list, outcome: dict, *, mutated: bool = False, dirty: bool = False) -> None:
        step = outcome['steps'][-1]
        self.assertFalse(outcome['ok'], outcome)
        self.assertNotIn('refusal_code', step, 'a technical fault is never a clean refusal')
        self.assertEqual((step.get('native_custody_held'), step['mutation_may_have_persisted']), (True, mutated), step)
        self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
        self.assertEqual(outcome['dirty'], dirty or mutated)
        self.assertEqual(after_fault(trace), [], 'no native call of any kind follows the fault')
        if not mutated:
            self.assertEqual(executes(inner), [], 'nothing was written')

    def test_get_default_that_raises_or_is_not_a_bool_holds_at_once(self) -> None:
        """Apply's HAction.GetDefault is BOOL; create's and change's legacy GetDefault is declared VT_I4 and answered the int 1 natively
        for both actions: anything but that int holds."""
        values = [raising(RuntimeError('COM failed')), raising(NativeNumberingRefusal('looks clean', code='style_not_found')),
                  returning(None), returning(0), returning(1), returning('True'), returning([True]), returning({'ok': True})]
        legacy = [raising(RuntimeError('COM failed')), raising(NativeNumberingRefusal('looks clean', code='style_not_found')),
                  returning(None), returning(True), returning(False), returning(0), returning(2), returning('1'), returning([1]), returning({'ok': True})]
        cases = [('apply', styles_document, 'call:HAction.GetDefault', effect) for effect in values]
        cases += [(op, document, 'call:CreateAction().GetDefault', effect) for op, document in (('create', styles_document), ('update', plain_document))
                  for effect in legacy]
        for number, (op, document, when, effect) in enumerate(cases):
            with self.subTest(op=op, value=number):
                owned, inner, trace = self.owned(document)
                request = self.request(owned, op)
                fault = Fault(when, effect)
                object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                outcome = bundle(owned, **request)
                self.assertTrue(fault.fired)
                self.assert_held(owned, inner, trace, outcome)

    def test_a_parameter_write_that_raises_holds_at_once(self) -> None:
        cases = (('apply', styles_document, 'set:HParameterSet.HStyle.Apply'),
                 ('create', styles_document, 'call:CreateAction().CreateSet().SetItem'),
                 ('create', styles_document, 'call:CreateAction().CreateSet().Item().SetItem'),
                 ('update', plain_document, 'call:CreateAction().CreateSet().SetItem'),
                 ('update', plain_document, 'call:CharShape.SetItem'))
        for op, document, when in cases:
            for exc in (RuntimeError('setter failed'), NativeNumberingRefusal('looks clean', code='style_name_taken')):
                with self.subTest(op=op, when=when, exc=type(exc).__name__):
                    owned, inner, trace = self.owned(document)
                    request = self.request(owned, op)
                    fault = Fault(when, raising(exc))
                    object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                    outcome = bundle(owned, **request)
                    self.assertTrue(fault.fired)
                    self.assert_held(owned, inner, trace, outcome)

    def test_a_malformed_caret_or_selection_after_placement_holds_at_once(self) -> None:
        cases = [(op, document, 'call:get_pos', value) for op, document in (('create', styles_document), ('update', plain_document), ('apply', styles_document))
                 for value in (None, (0, 1), ('0', 1, 0), (0, 1, -1), [0, 1, 0, 9])]
        cases += [(op, document, 'call:get_selected_pos', value) for op, document in (('create', styles_document), ('apply', styles_document))
                  for value in (None, (True, 0, 1, 0), (True, 0, 1, 0, 0, 1, 'x'))]
        cases += [('apply', styles_document, 'call:select_text', value) for value in (None, 1, 'True')]
        for op, document, when, value in cases:
            with self.subTest(op=op, when=when, value=value):
                owned, inner, trace = self.owned(document)
                request = self.request(owned, op)
                fault = Fault(when, returning(value), after='call:set_pos')
                object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                outcome = bundle(owned, **request)
                self.assertTrue(fault.fired)
                self.assert_held(owned, inner, trace, outcome)

    def test_a_raising_caret_read_after_placement_holds_at_once(self) -> None:
        for op, document in (('create', styles_document), ('update', plain_document), ('apply', styles_document)):
            for exc in (RuntimeError('GetPos failed'), NativeNumberingRefusal('looks clean', code='selection_unverified')):
                with self.subTest(op=op, exc=type(exc).__name__):
                    owned, inner, trace = self.owned(document)
                    request = self.request(owned, op)
                    fault = Fault('call:get_pos', raising(exc), after='call:set_pos')
                    object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                    self.assert_held(owned, inner, trace, bundle(owned, **request))

    def test_a_malformed_read_after_the_action_holds_at_once(self) -> None:
        for op, document in (('apply', styles_document), ('create', styles_document), ('update', plain_document)):
            for when, value in (('call:get_selected_pos', None), ('call:get_text_file', '')):
                with self.subTest(op=op, when=when):
                    owned, inner, trace = self.owned(document)
                    request = self.request(owned, op)
                    fault = Fault(when, returning(value), after='call:HAction.Execute' if op == 'apply' else 'call:CreateAction().Execute')
                    object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                    outcome = bundle(owned, **request)
                    self.assertTrue(fault.fired)
                    self.assert_held(owned, inner, trace, outcome, mutated=True)

    def test_after_an_earlier_dirty_edit_the_fault_holds_with_the_debt_kept(self) -> None:
        for op, document, when in (('apply', styles_document, 'set:HParameterSet.HStyle.Apply'), ('create', styles_document, 'call:CreateAction().GetDefault')):
            with self.subTest(op=op):
                owned, inner, trace = self.owned(document, dirty_first=True)
                request = self.request(owned, op)
                effect = raising(RuntimeError('fault')) if when.startswith('set:') else returning(True)
                fault = Fault(when, effect)
                object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                outcome = bundle(owned, **request)
                self.assertTrue(fault.fired)
                step = outcome['steps'][-1]
                self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False))
                self.assertNotIn('refusal_code', step)
                self.assertEqual(after_fault(trace), [])
                self.assertEqual(len(executes(inner)), 1, 'only the earlier edit was written')
                self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')

    def test_well_typed_answers_stay_clean_refusals_with_the_caret_given_back(self) -> None:
        cases = (('apply', styles_document, Fault('call:HAction.GetDefault', returning(False)), 'style_action_unavailable'),
                 # The legacy set holds no character shape to change after GetDefault: a well-typed BOOL False.
                 ('create', styles_document, Fault('call:CreateAction().CreateSet().ItemExist', returning(False)), 'style_variant_unsupported'),
                 ('apply', styles_document, Fault('call:select_text', returning(False), after='call:set_pos'), 'selection_unverified'),
                 # Both caret reads of the check answer the same well-typed other place: the caret is somewhere else, not unreadable.
                 ('create', styles_document, Fault('call:get_pos', returning((0, 1, 5)), after='call:set_pos', count=2), 'selection_unverified'))
        for op, document, fault, code in cases:
            with self.subTest(op=op, code=code, when=fault.when):
                owned, inner, trace = self.owned(document)
                request = self.request(owned, op)
                caret = inner.cursor
                object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                outcome = bundle(owned, **request)
                self.assertTrue(fault.fired)
                step = outcome['steps'][-1]
                self.assertEqual(step.get('refusal_code'), code, step)
                self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(executes(inner), [])
                self.assertEqual(inner.cursor, caret, 'the caret is given back')


if __name__ == '__main__':
    unittest.main()
