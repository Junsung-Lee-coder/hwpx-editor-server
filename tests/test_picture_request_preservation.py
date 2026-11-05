"""Prospective request proof through real service/STA; synthetic external SDK only."""
import copy, unittest
import xml.etree.ElementTree as ET
from app import native_picture as native
from tests.test_picture_n2_contract import Picture, owned, req, command, public_call
from tests.test_picture_mutation_contract import first, local

FLOWS = ('BothSides', 'LeftOnly', 'RightOnly', 'LargestOnly')

class NativeEffects(Picture):
    def __init__(self, *, effective=0, materialize=True, damage=None, shared=False):
        super().__init__(damage=damage, shared=shared)
        self.effective = effective
        self.materialize = materialize
        self.native_position = tuple(int(first(self.root, 'caretpos').get(k)) for k in ('List','Para','Pos'))
        first(self.root, 'shapeobject').attrib.pop('TextFlow', None)
        self.original_getpos_fault = None
        self.getpos_count = 0
    def GetPos(self):
        self.getpos_count += 1
        if self.getpos_count == 1 and self.original_getpos_fault is not None:
            return self.original_getpos_fault
        return self.native_position
    def GetDefault(self, name, pset):
        result = super().GetDefault(name, pset)
        if name == 'ShapeObjDialog':
            pset.root.values['TextFlow'] = self.effective
        return result
    def Execute(self, name, pset):
        result = super().Execute(name, pset)
        caret = first(self.root, 'caretpos')
        for key, value in zip(('List','Para','Pos'), self.native_position): caret.set(key,str(value))
        if name == 'ShapeObjDialog' and self.materialize:
            first(self.root,'shapeobject').set('TextFlow', FLOWS[self.effective] if type(self.effective) is int and 0 <= self.effective < 4 else 'UNKNOWN')
        # Post-state adversary must run after the honest serialization effects.
        if self.damage: self.damage(self)
        return result

class RequestPreservation(unittest.TestCase):
    def run_public(self, h, *, op='picture_layout_exact', fields=None):
        events=[]
        with owned(h) as o:
            o.service._picture_call_witness=events.append
            result,error=public_call(o,req(h,op,**(fields or {'mode':'square'})))
            held=o.session._quarantined
            cleanup=o.cleanup_events()
        return result,error,held,cleanup,events
    def assert_held(self,h,*,executed,op='picture_layout_exact',fields=None):
        result,error,held,cleanup,events=self.run_public(h,op=op,fields=fields)
        self.assertFalse(result is not None and result.get('ok') is True)
        self.assertTrue(held);self.assertEqual(cleanup,[])
        self.assertEqual(sum(e.startswith('Execute:') for e in h.events),executed)
        self.assertEqual(h.followons,[])
        return events
    def test_planned_caret_and_same_observed_flow_each_documented_enum(self):
        for value,label in enumerate(FLOWS):
            with self.subTest(flow=label):
                h=NativeEffects(effective=value,shared=True)
                result,error,held,cleanup,events=self.run_public(h)
                self.assertIsNone(error);self.assertTrue(result['ok'],result);self.assertFalse(held)
                proof=result['steps'][0]['result']['ui_position']
                self.assertEqual(proof['contract'],'selected_target_anchor')
                self.assertEqual(proof['original'],[0,0,32]);self.assertEqual(proof['expected'],[0,0,1])
                self.assertEqual(proof['serialized_after'],[0,0,1]);self.assertTrue(proof['moved'])
                flow=result['steps'][0]['result']['effective_text_flow']
                self.assertEqual(flow['observed_before'],value);self.assertEqual(flow['serialized_after'],label)
                self.assertTrue(flow['materialized']);self.assertEqual(flow['proof'],'same_observed_effective_value')
                self.assertFalse(any(e.startswith('put:HShapeObject.TextFlow') for e in h.events))
    def test_unchanged_raw_caret_and_absent_flow_are_reported_without_materialization(self):
        h=NativeEffects(materialize=False);h.native_position=(0,0,1)
        first(h.root,'caretpos').set('Pos','1')
        result,error,held,cleanup,events=self.run_public(h)
        self.assertIsNone(error);self.assertTrue(result['ok'],result)
        proof=result['steps'][0]['result']
        self.assertFalse(proof['ui_position']['moved'])
        self.assertFalse(proof['effective_text_flow']['materialized'])
    def test_wrong_exact_postcaret_and_extra_head_hold_after_execute(self):
        damages=[lambda h,k=k:first(h.root,'caretpos').set(k,'9') for k in ('List','Para','Pos')]
        damages += [lambda h:ET.SubElement(first(h.root,'docsetting'),'EXTRA'),
                    lambda h:ET.SubElement(first(h.root,'head'),'CARETPOS',List='0',Para='0',Pos='1'),
                    lambda h:first(h.root,'head').set('SecCnt','999')]
        for damage in damages:
            with self.subTest(damage=damages.index(damage)):
                self.assert_held(NativeEffects(damage=damage),executed=1)
    def test_missing_duplicate_malformed_or_unmatched_original_never_enters_mutator(self):
        changes=[lambda h:first(h.root,'caretpos').set('Pos','01'),
                 lambda h:first(h.root,'caretpos').set('List','1'),
                 lambda h:first(h.root,'caretpos').attrib.pop('Para'),
                 lambda h:ET.SubElement(first(h.root,'docsetting'),'CARETPOS',List='0',Para='0',Pos='32'),
                 lambda h:first(h.root,'docsetting').remove(first(h.root,'caretpos'))]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                h=NativeEffects();change(h);self.assert_held(h,executed=0)
                self.assertFalse(any(e.startswith('put:') for e in h.events))
    def test_missing_badtyped_or_unmatched_effective_flow_has_no_put_or_execute(self):
        for value in (None,True,'0',0.0,-1,4,65536):
            with self.subTest(value=type(value).__name__,repr=str(value)):
                h=NativeEffects(effective=value);events=self.assert_held(h,executed=0)
                self.assertFalse(any(e.startswith('put:') for e in h.events))
                self.assertIn('typed.HShapeObject.get.TextFlow',[e['phase'] for e in events])
    def test_changed_effective_flow_and_other_target_deltas_remain_strict(self):
        damages=[lambda h:first(h.root,'shapeobject').set('TextFlow','RightOnly'),
                 lambda h:first(h.root,'shapeobject').set('AllowOverlap','true'),
                 lambda h:first(h.root,'imageclip').set('Left','1'),
                 lambda h:first(h.root,'size').set('Width','17'),
                 lambda h:ET.SubElement(first(h.root,'picture'),'UNKNOWN_EFFECT')]
        for damage in damages:
            with self.subTest(damage=damages.index(damage)):
                self.assert_held(NativeEffects(damage=damage),executed=1)
    def test_existing_textflow_must_be_exact_and_agree_with_positive_prestate(self):
        h=NativeEffects(effective=1);first(h.root,'shapeobject').set('TextFlow','BothSides')
        self.assert_held(h,executed=0)
        h=NativeEffects(effective=0);first(h.root,'shapeobject').set('TextFlow','BothSides')
        result,error,held,cleanup,events=self.run_public(h)
        self.assertIsNone(error);self.assertTrue(result['ok'],result)
        self.assertFalse(result['steps'][0]['result']['effective_text_flow']['materialized'])

if __name__=='__main__':unittest.main()
