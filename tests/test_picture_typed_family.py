"""Typed native-family and complete ordinary scope, real public/STA seam.
Only external SDK/typeinfo observations are synthetic; no native qualification.
"""
import unittest,copy
from app import native_picture as picture
from tests.test_picture_n2_contract import Picture,owned,req,command,public_call
from tests.test_picture_mutation_contract import first,local
from tests.native_picture_typed_fakes import HSet,default

def singleton(h):
 for p in list(h.root.iter()):
  for c in list(p):
   if local(c) in ('header','footer'):p.remove(c)
 h.relink();return h
class TypedFamily(unittest.TestCase):
 def test_singleton_layout_reaches_one_typed_execute_without_legacy_factory(self):
  h=singleton(Picture());events=[]
  with owned(h) as o:
   o.service._picture_call_witness=events.append
   out=command(o,req(h,'picture_layout_exact',mode='square'))
   self.assertTrue(out['ok'],out)
  self.assertEqual([e for e in h.events if e.startswith('Execute:')],['Execute:ShapeObjDialog'])
  self.assertFalse(any(e.startswith('CreateAction:') for e in h.events))
  self.assertFalse(any(e.startswith('put:HDrawImageAttr') for e in h.events))
  self.assertFalse(any(e in ('put:HShapeObject.Width','put:HShapeObject.Height') for e in h.events))
 def test_rich_multi_picture_layout_preserves_other_and_all_crop_defaults(self):
  h=Picture(shared=True);before=picture.parse_pictures(h.get_text_file())
  with owned(h) as o:
   out=command(o,req(h,'picture_layout_exact',mode='square'));self.assertTrue(out['ok'],out)
  after=picture.parse_pictures(h.get_text_file())
  self.assertEqual(before['pictures'][0]['crop'],after['pictures'][0]['crop'])
  self.assertEqual(before['pictures'][1]['hash'],after['pictures'][1]['hash'])
 def test_typed_hset_and_legacy_create_set_are_not_interchangeable(self):
  h=singleton(Picture());h.selected=h.ctrls[0]
  legacy=h.CreateAction('ShapeObjDialog').CreateSet()
  with self.assertRaises(TypeError):default(h,'ShapeObjDialog',legacy)
  holder=h.HParameterSet.HShapeObject.HSet
  self.assertIs(type(holder),HSet);self.assertIs(default(h,'ShapeObjDialog',holder),True)
 def test_failed_default_is_first_unknown_no_parameter_put_or_execute(self):
  for bad in (False,None,1,'true'):
   with self.subTest(kind=type(bad).__name__):
    h=singleton(Picture());h.default_result=bad;events=[]
    with owned(h) as o:
     o.service._picture_call_witness=events.append
     out,error=public_call(o,req(h,'picture_layout_exact',mode='square'))
     self.assertFalse(out is not None and out.get('ok') is True)
     self.assertTrue(o.session._quarantined);self.assertEqual(o.cleanup_events(),[])
    self.assertFalse(any(e.startswith(('put:','Execute:')) for e in h.events))
    self.assertEqual(events[-1]['phase'],'HAction.GetDefault.ShapeObjDialog')
    self.assertEqual(events[-1]['stage'],'invalid_return')
 def test_malformed_typed_readback_is_held_without_execute(self):
  for bad in (True,'1',None,1.0):
   with self.subTest(kind=type(bad).__name__):
    h=singleton(Picture());h.typed_read_faults={('HShapeObject','TreatAsChar'):bad};events=[]
    with owned(h) as o:
     o.service._picture_call_witness=events.append
     out,error=public_call(o,req(h,'picture_layout_exact',mode='square'))
     self.assertFalse(out is not None and out.get('ok') is True)
     self.assertTrue(o.session._quarantined);self.assertEqual(o.cleanup_events(),[])
    self.assertFalse(any(e.startswith('Execute:') for e in h.events))
    self.assertEqual(events[-1]['stage'],'invalid_return')
 def test_wrong_root_interface_is_held_before_default(self):
  h=singleton(Picture());h.HParameterSet.HShapeObject._oleobj_.name='HPictureChange';events=[]
  with owned(h) as o:
   o.service._picture_call_witness=events.append
   out,error=public_call(o,req(h,'picture_layout_exact',mode='square'))
   self.assertFalse(out is not None and out.get('ok') is True)
   self.assertTrue(o.session._quarantined)
  self.assertFalse(any(e.startswith(('GetDefault:','put:','Execute:')) for e in h.events))
 def test_complete_native_picture_census_cannot_drop_another_picture(self):
  h=Picture(shared=True);h.ctrls[0].Next=None;events=[]
  with owned(h) as o:
   o.service._picture_call_witness=events.append
   out,error=public_call(o,req(h,'picture_layout_exact',mode='square'))
   self.assertFalse(out is not None and out.get('ok') is True)
   self.assertTrue(o.session._quarantined);self.assertEqual(o.cleanup_events(),[])
  self.assertFalse(any(e.startswith(('Run:SelectCtrlFront','Execute:')) for e in h.events))
if __name__=='__main__':unittest.main()
