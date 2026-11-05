"""Anchored Front route at real public service/owning-STA seam.
Synthetic SDK/OS only; this is not native qualification. Existing rich/shared
positives remain unchanged and mandatory, not replaced by these bounded cases.
"""
import collections
import unittest
import xml.etree.ElementTree as ET

from tests.test_picture_n2_contract import Picture, owned, req, command, public_call


class Anchor:
    def __init__(self, owner):
        self.owner = owner

    def Item(self, name):
        return self.owner.observe('anchor.Item.' + name,
                                  {'List': 0, 'Para': 3, 'Pos': 7}[name])


class NativeControl:
    def __init__(self, owner, kind, ident=None):
        self.owner, self.kind, self.ident = owner, kind, ident
        self.next_control = None

    @property
    def CtrlID(self):
        return self.owner.observe('control.CtrlID.' + self.kind, self.kind)

    @property
    def Next(self):
        return self.owner.observe('control.Next.' + self.kind, self.next_control)

    def GetCtrlInstID(self):
        return self.owner.observe('control.GetCtrlInstID', self.ident)

    def GetAnchorPos(self, option):
        self.owner.anchor_options.append(option)
        return self.owner.observe('target.GetAnchorPos', Anchor(self.owner))


class AnchoredPicture(Picture):
    def __init__(self, fault_phase=None, fault_value=None, fault_at=1):
        self.trace = []
        self.after_failed = []
        self.counts = collections.Counter()
        self.failed_observation = False
        self.fault_phase, self.fault_value, self.fault_at = fault_phase, fault_value, fault_at
        self.anchor_options = []
        self.position = (0, 0, 0)
        self.parent_value = None
        super().__init__()
        self.position = self.native_position
        # The retained portable fixture has header/footer controls. This NEW
        # synthetic bounded fixture deliberately removes them before either
        # predecessor RED or candidate GREEN, never relabels it a rich fixture.
        for parent in self.root.iter():
            for child in list(parent):
                if child.tag.rsplit('}', 1)[-1].lower() in ('header', 'footer'):
                    parent.remove(child)
        ident = next(e for e in self.root.iter()
                     if e.tag.rsplit('}', 1)[-1].lower() == 'shapeobject').get('InstId')
        self.raw_target = NativeControl(self, 'gso', ident)
        self.native_controls = [NativeControl(self, 'secd'), NativeControl(self, 'cold'), self.raw_target]
        self.link_controls()

    def link_controls(self):
        for i, ctrl in enumerate(self.native_controls):
            ctrl.next_control = self.native_controls[i + 1] if i + 1 < len(self.native_controls) else None
        self.HeadCtrl = self.native_controls[0] if self.native_controls else None

    def observe(self, phase, value):
        self.trace.append(phase)
        if self.failed_observation:
            self.after_failed.append(phase)
        self.counts[phase] += 1
        if phase == self.fault_phase and self.counts[phase] == self.fault_at:
            self.failed_observation = True
            if isinstance(self.fault_value, BaseException):
                raise self.fault_value
            return self.fault_value
        return value

    def get_text_file(self, *args):
        value = super().get_text_file(*args)
        return self.observe('after.GetTextFile' if self.executed else 'before.GetTextFile', value)

    def get_pos(self):
        self.observe('wrapper.get_pos', None)
        return super().get_pos()

    def SetPosBySet(self, anchor):
        result = self.observe('SetPosBySet', True)
        if result is True:
            self.position = (0, 3, 7)
            for key,value in zip(('List','Para','Pos'),self.position):
                next(e for e in self.root.iter() if e.tag.lower()=='caretpos').set(key,str(value))
        return result

    def GetPos(self):
        return self.observe('GetPos', self.position)

    def Run(self, name):
        result = self.observe('HAction.Run.' + name, True)
        if result is True and name == 'SelectCtrlFront':
            self.selected = self.raw_target
        return result

    def SelectCtrl(self, *args):
        self.observe('FORBIDDEN.SelectCtrl', None)
        return super().SelectCtrl(*args)

    def FindCtrl(self):
        self.observe('FORBIDDEN.FindCtrl', None)
        raise AssertionError('FindCtrl is not part of this contract')

    @property
    def CurSelectedCtrl(self):
        return self.observe('CurSelectedCtrl', self.selected)

    @property
    def ParentCtrl(self):
        return self.observe('ParentCtrl', self.parent_value)

    @ParentCtrl.setter
    def ParentCtrl(self, value):
        self.parent_value = value

    def CreateAction(self, name):
        self.observe('CreateAction', None)
        return super().CreateAction(name)

    def GetDefault(self, name, params):
        self.observe('GetDefault', None)
        return super().GetDefault(name, params)

    def Execute(self, name, params):
        result = self.observe('Execute', True)
        return super().Execute(name, params) if result is True else result


class AnchoredSelection(unittest.TestCase):
    def assert_held_without_followon(self, h, events, phase):
        with owned(h) as o:
            o.service._picture_call_witness = events.append
            result, error = public_call(o, req(h, 'picture_layout_exact', mode='square'))
            self.assertTrue(error is not None or (type(result) is dict and result['ok'] is False))
            self.assertTrue(o.session._quarantined)
            self.assertTrue(o.session._thread.is_alive())
            self.assertEqual(o.cleanup_events(), [])
        self.assertEqual(h.after_failed, [])
        self.assertEqual(h.counts['Execute'], 0)
        self.assertEqual(events[-1]['phase'], phase)

    def test_singleton_nonnoop_public_edit_one_front_one_execute_no_legacy_selector(self):
        h = AnchoredPicture()
        events = []
        with owned(h) as o:
            o.service._picture_call_witness = events.append
            result = command(o, req(h, 'picture_layout_exact', mode='square'))
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['steps'][0]['result']['changed'])
        self.assertEqual(h.anchor_options, [0])
        self.assertEqual(h.counts['SetPosBySet'], 1)
        # Original-caret proof, exact anchor readback and pre-Execute validation.
        self.assertEqual(h.counts['GetPos'], 3)
        self.assertEqual(h.counts['HAction.Run.SelectCtrlFront'], 1)
        self.assertEqual(h.counts['Execute'], 1)
        self.assertFalse(any(x.startswith('FORBIDDEN.') or 'Reverse' in x for x in h.trace))
        before = [e for e in events if e['stage'] == 'before']
        self.assertEqual([e['arguments'] for e in before if e['phase'] == 'SetPosBySet.target_anchor'],
                         [{'native_id': h.raw_target.ident, 'anchor': [0, 3, 7]}])
        self.assertEqual([e['arguments'] for e in before if e['phase'] == 'HAction.Run.SelectCtrlFront'],
                         [{'native_id': h.raw_target.ident, 'anchor': [0, 3, 7], 'action': 'SelectCtrlFront'}])

    def test_setpos_and_front_nonexact_returns_and_exceptions_stop_at_first_unknown(self):
        for fault, phase in [('SetPosBySet', 'SetPosBySet.target_anchor'),
                             ('HAction.Run.SelectCtrlFront', 'HAction.Run.SelectCtrlFront')]:
            for value in (False, None, 1, 'true', RuntimeError('external fault')):
                with self.subTest(fault=fault, value=type(value).__name__):
                    h = AnchoredPicture(fault, value)
                    self.assert_held_without_followon(h, [], phase)

    def test_null_and_malformed_anchor_fields_stop_before_next_sdk(self):
        cases = [('target.GetAnchorPos', None, 'target.GetAnchorPos(0)'),
                 ('target.GetAnchorPos', {}, 'target.GetAnchorPos(0)')]
        for key in ('List', 'Para', 'Pos'):
            for value in (None, True, 1.0, '0', -1, 2**31, RuntimeError('anchor Item fault')):
                cases.append(('anchor.Item.' + key, value, 'anchor.Item.' + key))
        cases.append(('anchor.Item.List', 1, 'anchor.Item.List'))
        for fault, value, phase in cases:
            with self.subTest(fault=fault, value=type(value).__name__):
                self.assert_held_without_followon(AnchoredPicture(fault, value), [], phase)

    def test_getpos_requires_exact_tuple_or_list_and_exact_anchor_equality(self):
        for value in (None, True, (0, 3), [0, 3, True], [0, 3, 7.0], [0, 3, 8],
                      RuntimeError('position fault')):
            with self.subTest(value=type(value).__name__):
                self.assert_held_without_followon(AnchoredPicture('GetPos', value, fault_at=2), [],
                                                 'GetPos.target_anchor')

    def test_target_native_id_is_rechecked_after_anchor_without_followon(self):
        h = AnchoredPicture('control.GetCtrlInstID', '999999', fault_at=2)
        self.assert_held_without_followon(h, [], 'target.GetCtrlInstID.after_anchor')

    def test_selected_parent_or_identity_fault_prevents_parameter_preparation(self):
        for fault, value, at, phase in [
                # First getter proves the preselection is empty; inject at selected getter.
                ('CurSelectedCtrl', None, 2, 'CurSelectedCtrl'),
                ('control.GetCtrlInstID', '999999', 3, 'selected.GetCtrlInstID'),
                ('control.CtrlID.gso', 'tbl', 2, 'selected.CtrlID'),
                ('ParentCtrl', object(), 1, 'ParentCtrl.body')]:
            with self.subTest(fault=fault):
                self.assert_held_without_followon(AnchoredPicture(fault, value, at), [], phase)

    def test_complete_native_universe_rejects_unknown_or_partial_census(self):
        for case in ('unknown', 'filtered', 'second_gso'):
            with self.subTest(case=case):
                h = AnchoredPicture()
                if case == 'unknown':
                    h.native_controls.insert(0, NativeControl(h, 'mystery'))
                elif case == 'filtered':
                    h.native_controls = [h.raw_target]
                else:
                    h.native_controls.append(NativeControl(h, 'gso', '999999'))
                h.link_controls()
                with owned(h) as o:
                    result, error = public_call(o, req(h, 'picture_layout_exact', mode='square'))
                    self.assertTrue(error is not None or result['ok'] is False)
                self.assertEqual(h.counts['SetPosBySet'], 0)
                self.assertEqual(h.counts['HAction.Run.SelectCtrlFront'], 0)
                self.assertEqual(h.counts['Execute'], 0)

    def test_parsed_unknown_or_locked_object_refuses_before_selector(self):
        for case in ('unknown', 'locked', 'header'):
            with self.subTest(case=case):
                h = AnchoredPicture()
                if case == 'locked':
                    next(e for e in h.root.iter() if e.tag.lower() == 'shapeobject').set('Lock', 'true')
                else:
                    ET.SubElement(next(e for e in h.root.iter() if e.tag.lower() == 'body'),
                                  'UNKNOWNCONTROL' if case == 'unknown' else 'HEADER')
                with owned(h) as o:
                    result, error = public_call(o, req(h, 'picture_layout_exact', mode='square'))
                    self.assertTrue(error is not None or result['ok'] is False)
                self.assertEqual(h.counts['SetPosBySet'], 0)
                self.assertEqual(h.counts['Execute'], 0)

    def test_extra_shapeobject_outside_picture_is_not_a_complete_singleton(self):
        h = AnchoredPicture()
        body = next(e for e in h.root.iter() if e.tag.lower() == 'body')
        ET.SubElement(body, 'SHAPEOBJECT', InstId='999999', Lock='false')
        with owned(h) as o:
            result, error = public_call(o, req(h, 'picture_layout_exact', mode='square'))
            self.assertTrue(error is not None or result['ok'] is False)
        self.assertEqual(h.counts['Execute'], 0)

    def test_malformed_postexecute_preserves_issued_custody_no_further_sdk(self):
        h = AnchoredPicture('after.GetTextFile', None)
        events = []
        with owned(h) as o:
            o.service._picture_call_witness = events.append
            result, error = public_call(o, req(h, 'picture_layout_exact', mode='square'))
            self.assertTrue(error is not None or result['ok'] is False)
            self.assertTrue(o.session._quarantined)
            self.assertEqual(o.cleanup_events(), [])
        self.assertEqual(h.counts['Execute'], 1)
        self.assertEqual(h.after_failed, [])
        self.assertEqual(events[-1]['phase'], 'after.GetTextFile.HWPML2X')


if __name__ == '__main__':
    unittest.main()
