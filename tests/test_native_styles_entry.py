"""Named styles: the first native reads of an edit or inventory, before any navigation.

- **Capabilities:** apply reads HAction, HParameterSet, HStyle and HAction's GetDefault/Execute through the wrapper's COM
  getters; create and change read the legacy family instead: CreateAction, the action it returns with its CreateSet,
  GetDefault and Execute, and the set CreateSet returns with its SetItem, and for a create ItemExist and Item when a character
  property is requested; a change reads IHwpObject.CharShape with its ItemExist, Item and SetItem first, at the style's use. The methods the edit calls (set_pos at the first placement, MoveDocBegin and find for a table cell) go through
  the same lookup. One that raises, returns None or a plain value, or returns a method that cannot be called holds the
  session at once, with no further native call. Only an attribute that is absent without any native entry is the clean
  native_unavailable refusal.
- **Document text:** a get_text_file('HWPML2X') reply that is not a str, is empty or is not well-formed XML holds at once,
  at the service's first read, the engine's read and the inventory. A well-formed document the style model cannot represent
  is still the clean style_model_unusable refusal.

Through the real owned service, with every attribute, call and write on the SDK object recorded (tests.test_native_styles_repairs).
Not Hancom evidence.
"""
from __future__ import annotations

import unittest

from tests.native_styles_fakes import StyleHwp, styles_document
from tests import test_native_styles_repairs as repairs
from tests.test_native_styles import APPLY, executes, plain_document
from tests.test_native_styles_repairs import Fault, after_fault, bundle, raising, returning
from tests.test_repair_runtime_safety import owned_doc

NOT_XML = '<HWPML><HEAD></HWPML'
GETTER_FAULTS = (('raises', raising(RuntimeError('COM getter failed'))), ('absent', raising(AttributeError('dynamic lookup'))), ('none', returning(None)),
                 ('plain', returning(1)))
SETS = {'apply': 'HStyle'}
LEGACY = 'CreateAction().'
LEGACY_GETTERS = {
    'create': ('get:CreateAction', 'call:CreateAction', f'get:{LEGACY}CreateSet', f'get:{LEGACY}GetDefault', f'get:{LEGACY}Execute', f'call:{LEGACY}CreateSet',
               f'get:{LEGACY}CreateSet().SetItem', f'get:{LEGACY}CreateSet().ItemExist', f'get:{LEGACY}CreateSet().Item'),
    'update': ('get:CharShape', 'get:CharShape.ItemExist', 'get:CharShape.Item', 'get:CharShape.SetItem',
               'get:CreateAction', 'call:CreateAction', f'get:{LEGACY}CreateSet', f'get:{LEGACY}GetDefault', f'get:{LEGACY}Execute', f'call:{LEGACY}CreateSet',
               f'get:{LEGACY}CreateSet().SetItem'),
}


class PlainAction:
    """A legacy action object with no CreateSet at all: a plain object, no descriptor and no lookup hook."""

    def GetDefault(self, pset: object) -> None:  # noqa: N802
        raise AssertionError('never reached')

    def Execute(self, pset: object) -> None:  # noqa: N802
        raise AssertionError('never reached')
DOCUMENTS = {'apply': styles_document, 'create': styles_document, 'update': plain_document, 'inventory': styles_document}


class DeclaredGetters(StyleHwp):
    """The pinned wrapper's own shape (pyhwpx 1.7.2 core.py 1248-1268, 1302-1319): HAction and HParameterSet are declared
    properties, getters into the COM object, and the class has no __getattr__. ``failing`` names the getter that raises
    AttributeError, as a COM getter failing on entry does; ``log`` records the getters and the native methods called."""

    failing: str | None = None


def _getter(name: str) -> property:
    def get(self: DeclaredGetters) -> object:
        self.log.append('get:' + name)
        if self.failing == name:
            self.log.append('FAULT:get:' + name)
            raise AttributeError(name)
        return self.__dict__['_' + name]

    def put(self: DeclaredGetters, value: object) -> None:
        self.__dict__['_' + name] = value
    return property(get, put)


def _logged(name: str):
    def call(self: DeclaredGetters, *args, **kwargs):
        self.log.append('call:' + name)
        return getattr(StyleHwp, name)(self, *args, **kwargs)
    return call


for _name in ('HAction', 'HParameterSet'):
    setattr(DeclaredGetters, _name, _getter(_name))
for _name in ('get_text_file', 'get_pos', 'set_pos', 'get_selected_pos', 'select_text', 'MoveDocBegin', 'find'):
    setattr(DeclaredGetters, _name, _logged(_name))


def declared_getters(document) -> DeclaredGetters:
    hwp = document()
    hwp.__class__ = DeclaredGetters
    for name in ('HAction', 'HParameterSet'):
        hwp.__dict__['_' + name] = hwp.__dict__.pop(name)
    hwp.log = []
    return hwp


def reorder_ids(owner) -> None:
    """A well-formed document whose style ids are not the list order: one the style model cannot represent."""
    real = owner._style_xml  # noqa: SLF001
    owner._style_xml = lambda chars, paras: real(chars, paras).replace('Id="1"', 'Id="7"', 1)  # noqa: SLF001


class _Entry(unittest.TestCase):
    owned = repairs.Boundary.owned
    request = repairs.Boundary.request
    assert_held = repairs.Boundary.assert_held

    def faulted(self, op: str, fault: Fault, *, dirty_first: bool = False):
        owned, inner, trace = self.owned(DOCUMENTS[op], dirty_first=dirty_first)
        request = self.request(owned, op) if op != 'inventory' else {'op': 'style_inventory'}
        object.__getattribute__(owned.hwp, '_t')[2].append(fault)
        outcome = bundle(owned, **request)
        self.assertTrue(fault.fired, f'the fault at {fault.when} was reached')
        return owned, inner, trace, outcome


class Capabilities(_Entry):
    def test_a_capability_getter_that_raises_or_returns_none_holds_at_once(self) -> None:
        """Each getter is entered through the traced object's dynamic lookup, as through COM: even an AttributeError is not absence."""
        cases = [('apply', when) for when in ('get:HAction', 'get:HParameterSet', 'get:HParameterSet.HStyle', 'get:HAction.GetDefault', 'get:HAction.Execute')]
        cases += [(op, when) for op, getters in LEGACY_GETTERS.items() for when in getters]
        for op, when in cases:
            effects = (('raises', raising(RuntimeError('COM getter failed'))), ('absent', raising(AttributeError(when))), ('none', returning(None)))
            for name, effect in effects:
                with self.subTest(op=op, when=when, effect=name):
                    owned, inner, trace, outcome = self.faulted(op, Fault(when, effect))
                    self.assert_held(owned, inner, trace, outcome)

    def test_a_returned_object_that_is_not_the_capability_holds_at_once(self) -> None:
        cases = [('apply', 'get:HAction', 'HAction'), ('apply', 'get:HParameterSet', 0), ('apply', 'get:HAction.GetDefault', 0),
                 ('apply', 'get:HAction.Execute', 'Execute'), ('apply', 'get:HParameterSet.HStyle', '')]
        for op in ('create', 'update'):
            cases += [(op, 'get:CreateAction', 'CreateAction'), (op, 'call:CreateAction', 'StyleAddNew'), (op, f'get:{LEGACY}CreateSet', 1),
                      (op, f'get:{LEGACY}GetDefault', True), (op, f'get:{LEGACY}Execute', b'Execute'), (op, f'call:{LEGACY}CreateSet', ()),
                      (op, f'get:{LEGACY}CreateSet().SetItem', 'SetItem')]
        for op, when, value in cases:
            with self.subTest(op=op, when=when, value=value):
                owned, inner, trace, outcome = self.faulted(op, Fault(when, returning(value)))
                self.assert_held(owned, inner, trace, outcome)

    def test_the_caret_method_read_at_the_first_placement_holds_at_once(self) -> None:
        """set_pos is the first native member an edit reads once it has entered navigation."""
        for op in ('apply', 'create', 'update'):
            for name, effect in GETTER_FAULTS:
                with self.subTest(op=op, effect=name):
                    owned, inner, trace, outcome = self.faulted(op, Fault('get:set_pos', effect))
                    self.assert_held(owned, inner, trace, outcome)

    def test_a_table_cell_search_method_read_before_entry_holds_at_once(self) -> None:
        for when in ('get:MoveDocBegin', 'get:find'):
            for name, effect in GETTER_FAULTS:
                with self.subTest(when=when, effect=name):
                    owned, inner, trace = self.owned(DOCUMENTS['apply'])
                    found = bundle(owned, op='style_inventory', name='본문')['steps'][0]['result']['styles'][0]
                    request = {**APPLY, 'style_name': '본문', 'expected_style_hash': found['hash'], 'paragraph_index': 6, 'expected_text': '셀 둘'}
                    fault = Fault(when, effect)
                    object.__getattribute__(owned.hwp, '_t')[2].append(fault)
                    outcome = bundle(owned, **request)
                    self.assertTrue(fault.fired, when)
                    self.assert_held(owned, inner, trace, outcome)

    def test_after_an_earlier_dirty_edit_an_entry_fault_holds_with_the_debt_kept(self) -> None:
        for op, fault in (('apply', Fault('get:HParameterSet', returning(None))), ('apply', Fault('call:get_text_file', returning(''))),
                          ('create', Fault('call:CreateAction', returning(None))), ('create', Fault(f'call:{LEGACY}CreateSet', raising(RuntimeError('COM failed'))))):
            with self.subTest(op=op, when=fault.when):
                owned, inner, trace, outcome = self.faulted(op, fault, dirty_first=True)
                step = outcome['steps'][-1]
                self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False))
                self.assertNotIn('refusal_code', step)
                self.assertEqual(after_fault(trace), [])
                self.assertEqual(len(executes(inner)), 1, 'only the earlier edit was written')
                self.assertTrue(owned.store.get('working_copy_dirty') or owned.store.get('dirty'), 'the earlier edit is still unsaved debt')

    def test_a_declared_getter_that_raises_attribute_error_holds_at_once(self) -> None:
        """No proxy and no __getattr__: inspect.getattr_static finds the declared property, so its AttributeError is not absence.
        Apply reads these two getters; create and change read none of them."""
        for name in ('HAction', 'HParameterSet'):
            with self.subTest(getter=name):
                hwp = declared_getters(DOCUMENTS['apply'])
                owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                request = self.request(owned, 'apply')
                caret = hwp.cursor
                hwp.failing = name
                outcome = bundle(owned, **request)
                step = outcome['steps'][-1]
                self.assertIn('FAULT:get:' + name, hwp.log, 'the fault was reached')
                self.assertEqual(hwp.log[hwp.log.index('FAULT:get:' + name) + 1:], [], 'no native getter or call follows the fault')
                self.assertNotIn('refusal_code', step)
                self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
                self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(hwp.cursor, caret, 'the caret never moved')
                self.assertEqual(executes(hwp), [], 'nothing was written')
        for op in ('create', 'update'):
            with self.subTest(op=op):
                hwp = declared_getters(DOCUMENTS[op])
                owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                request = self.request(owned, op)
                hwp.failing = 'HAction'
                self.assertTrue(bundle(owned, **request)['ok'], 'create and change never read HAction or HParameterSet')
                self.assertEqual([item for item in hwp.log if item.startswith(('get:HAction', 'get:HParameterSet'))], [])

    def test_a_wrapper_without_the_attribute_at_all_is_the_clean_refusal(self) -> None:
        """A plain wrapper object with no such attribute, no descriptor and no dynamic lookup: for apply and create reading it entered
        nothing native, so it is the clean refusal. A change reads its legacy family only after placing the caret and reading the style's
        shape, so there the same absence holds (below; tests.test_native_styles_typed_reads)."""
        for op, missing in (('apply', 'HAction'), ('apply', 'HParameterSet.HStyle'), ('create', 'CreateSet')):
            with self.subTest(op=op, missing=missing):
                hwp = DOCUMENTS[op]()
                owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                request = self.request(owned, op)
                caret = hwp.cursor
                if missing == 'CreateSet':
                    hwp.CreateAction = lambda name: PlainAction()
                else:
                    *path, name = missing.split('.')
                    owner = hwp
                    for part in path:
                        owner = getattr(owner, part)
                    delattr(owner, name)
                step = bundle(owned, **request)['steps'][-1]
                self.assertEqual(step.get('refusal_code'), 'native_unavailable', step)
                self.assertFalse(step['mutation_may_have_persisted'])
                self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(executes(hwp), [])
                self.assertEqual(hwp.cursor, caret, 'the caret never moved')
        with self.subTest(op='update', missing='CreateSet'):
            hwp = DOCUMENTS['update']()
            owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
            request = self.request(owned, 'update')
            hwp.CreateAction = lambda name: PlainAction()
            step = bundle(owned, **request)['steps'][-1]
            self.assertNotIn('refusal_code', step, 'after entry an absence is never the clean native_unavailable')
            self.assertEqual((step.get('native_custody_held'), step['mutation_may_have_persisted']), (True, False), step)
            self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
            self.assertEqual(executes(hwp), [])
            read = max(number for number, call in enumerate(hwp.calls) if call == ('CharShape', 'get'))
            self.assertEqual([call for call in hwp.calls[read + 1:] if call[0] == 'set_pos'], [], 'the caret is not given back after the hold')


class DocumentText(_Entry):
    VALUES = (('empty', ''), ('blank', '  \r\n'), ('none', None), ('bytes', b'<HWPML/>'), ('not_xml', NOT_XML))

    def test_a_malformed_reply_at_the_services_first_read_or_the_engines_read_holds_at_once(self) -> None:
        for op in ('apply', 'create', 'update'):
            for skip, where in ((0, 'service'), (1, 'engine')):
                for name, value in self.VALUES:
                    with self.subTest(op=op, read=where, value=name):
                        owned, inner, trace, outcome = self.faulted(op, Fault('call:get_text_file', returning(value), skip=skip))
                        self.assert_held(owned, inner, trace, outcome)

    def test_a_malformed_inventory_reply_holds_at_once(self) -> None:
        for name, value in self.VALUES:
            with self.subTest(value=name):
                owned, inner, trace, outcome = self.faulted('inventory', Fault('call:get_text_file', returning(value)))
                self.assert_held(owned, inner, trace, outcome)

    def test_a_well_formed_document_the_model_cannot_represent_stays_a_clean_refusal(self) -> None:
        for op in ('inventory', 'apply'):
            with self.subTest(op=op):
                hwp = styles_document()
                owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                request = self.request(owned, op) if op != 'inventory' else {'op': 'style_inventory'}
                reorder_ids(hwp)
                step = bundle(owned, **request)['steps'][-1]
                self.assertEqual(step.get('refusal_code'), 'style_model_unusable', step)
                self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(executes(hwp), [])

    def test_the_seed_and_a_normal_edit_still_pass(self) -> None:
        owned, inner, _trace = self.owned(styles_document)
        result = bundle(owned, op='style_inventory')
        self.assertTrue(result['ok'], result)
        self.assertTrue(bundle(owned, **self.request(owned, 'apply'))['ok'])
        self.assertEqual(len(executes(inner)), 1)


if __name__ == '__main__':
    unittest.main()
