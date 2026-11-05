"""Additional exact-boundary falsifiers on integrated source; not pre-edit RED."""
import unittest
import xml.etree.ElementTree as ET
from tests import test_picture_request_preservation as sdk
from tests.test_picture_mutation_contract import first

class Sequence(dict):
    def __init__(self,values):super().__init__();self.sequence=iter(values)
    def get(self,key,default=None):
        return next(self.sequence) if key==('HShapeObject','TextFlow') else default

class RequestAdversaries(unittest.TestCase):
    run_public=sdk.RequestPreservation.run_public
    assert_held=sdk.RequestPreservation.assert_held
    def test_original_native_coordinate_invalidity_has_no_later_sdk(self):
        for bad in (True,'0',(0,0),[0,0,True],(0,0,2**31)):
            with self.subTest(kind=type(bad).__name__):
                h=sdk.NativeEffects();h.original_getpos_fault=bad
                events=self.assert_held(h,executed=0)
                self.assertEqual(events[-1]['phase'],'GetPos.original')
                self.assertEqual(events[-1]['stage'],'invalid_return')
                self.assertEqual(h.events,['GetTextFile'])
    def test_preexecute_position_drift_stops_before_execute_and_no_next_sdk(self):
        class Drift(sdk.NativeEffects):
            armed=False
            def GetPos(self):
                value=super().GetPos()
                return (0,0,99) if self.armed else value
        h=Drift();events=[]
        with sdk.owned(h) as o:
            def observe(row):
                events.append(row)
                if row['phase']=='GetPos.preExecute' and row['stage']=='before':h.armed=True
            o.service._picture_call_witness=observe
            result,error=sdk.public_call(o,sdk.req(h,'picture_layout_exact',mode='square'))
            self.assertFalse(result is not None and result.get('ok') is True)
            self.assertTrue(o.session._quarantined);self.assertEqual(o.cleanup_events(),[])
        self.assertFalse(any(e.startswith('Execute:') for e in h.events))
        self.assertEqual(events[-1]['phase'],'GetPos.preExecute')
        self.assertEqual(events[-1]['stage'],'invalid_proof')
    def test_preexecute_effective_flow_drift_has_no_later_selection_or_execute(self):
        h=sdk.NativeEffects();h.typed_read_faults=Sequence((0,1))
        events=self.assert_held(h,executed=0)
        self.assertEqual(events[-1]['phase'],'proof.TextFlow.preExecute')
        self.assertEqual(events[-1]['stage'],'invalid_proof')
        self.assertEqual(h.events[-1],'get:HShapeObject.TextFlow')
    def test_textflow_allowance_never_covers_another_picture_or_crop(self):
        def other_picture(h):
            shapes=[e for e in h.root.iter() if e.tag.lower()=='shapeobject']
            shapes[1].set('TextFlow','RightOnly')
        self.assert_held(sdk.NativeEffects(shared=True,damage=other_picture),executed=1)
        h=sdk.NativeEffects()
        self.assert_held(h,executed=1,op='picture_crop_exact',fields={'left':.25,'top':0.,'right':.75,'bottom':1.})
    def test_foreign_caret_location_cannot_authorize_even_exact_coordinates(self):
        h=sdk.NativeEffects();element=first(h.root,'caretpos')
        first(h.root,'docsetting').remove(element);first(h.root,'text').append(element)
        events=self.assert_held(h,executed=0)
        self.assertEqual(events[-1]['phase'],'proof.ui.original_serialized')

if __name__=='__main__':unittest.main()
