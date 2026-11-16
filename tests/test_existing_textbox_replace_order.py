"""Synthetic structural source regression, NOT native25 HML/SDK acceptance."""
import unittest,xml.etree.ElementTree as ET
from unittest.mock import patch
from tests.test_native_container_identity import setup,NATIVE
from tests.native_census_state_review_models import Control,link
from tests.test_empty_ctrlid_product import Empty
from tests.native_text_containers_fakes import ContainersHwp
from app import native_text_containers as nt
class ExactReplacement(unittest.TestCase):
 def test_exact_selection_is_deleted_before_insert_without_doubled_text_layout(self):
  own,_,request=setup(self);h=own.hwp;events=[];old=h.GetTextFile;original=request['expected_text'];seen=[]
  def export(fmt='HWPML2X',option=''):
   value=old(fmt,option)
   if not option:
    root=ET.fromstring(value);box=next(root.iter('RECTANGLE'));text=''.join(c.text or''for c in box.iter('CHAR'))
    if len(text)>max(len(original),len(request['new_text'])):
     seen.append(True);next(box.iter('SHAPECOMPONENT')).set('CurHeight','6366');return ET.tostring(root,encoding='unicode')
   return value
  h.GetTextFile=export
  with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))):r,e=own.run(request)
  self.assertIsNone(e);self.assertTrue(r['ok'],'replacement must not produce a double-content intermediate extent');self.assertFalse(seen);self.assertFalse(own.session._native_custody_held);self.assertTrue(r['dirty']);ops=[c[:2]for c in h.calls if c[0]in('Execute','Run')and c[1]in('InsertText','Delete')];self.assertEqual(ops,[('Run','Delete'),('Execute','InsertText')])
 def test_real_target_shape_drift_still_refuses_after_insert(self):
  own,_,request=setup(self);h=own.hwp;events=[];old=h.GetTextFile;execute=h.execute;inserted=[];fault=[];hold=nt._Native.hold
  def action(name,items):
   result=execute(name,items)
   if name=='InsertText':inserted.append(True)
   return result
  def export(fmt='HWPML2X',option=''):
   value=old(fmt,option)
   if inserted and not option:
    root=ET.fromstring(value);next(root.iter('SHAPECOMPONENT')).set('CurHeight','6366');return ET.tostring(root,encoding='unicode')
   return value
  def latch(native,site,returned='invalid'):
   fault.append(len(h.calls));return hold(native,site,returned)
  h.execute=action;h.GetTextFile=export
  with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))),patch.object(nt._Native,'hold',latch):r,e=own.run(request)
  self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(own.session._native_custody_held);self.assertTrue(inserted);self.assertTrue(fault);self.assertEqual(len(h.calls),fault[0])
 def test_first_delete_fault_or_unproved_empty_state_has_zero_next_insert_or_SDK(self):
  for case in('false','integer','raised','malformed','other_document','shape','inband_sentinel'):
   with self.subTest(case=case):
    own,_,request=setup(self,session_id='custody-'+case.replace('_','-')+'-0001');h=own.hwp;events=[];run=h.HAction.Run;export=h.GetTextFile;deleted=[];stops=[]
    def action(name):
     value=run(name)
     if name=='Delete':
      deleted.append(True)
      if case in('false','integer','raised'):
       stops.append(len(h.calls))
       if case=='raised':raise RuntimeError('synthetic Delete SDK fault')
       return False if case=='false'else 1
     return value
    def read(fmt='HWPML2X',option=''):
     value=export(fmt,option)
     if deleted and not option:
      stops.append(len(h.calls))
      if case=='malformed':return '<HWPML'
      root=ET.fromstring(value)
      if case=='other_document':root.find('HEAD').set('UnexpectedDocument','changed')
      elif case=='shape':next(root.iter('SHAPECOMPONENT')).set('CurHeight','6366')
      elif case=='inband_sentinel':next(root.iter('RECTANGLE')).set('qualified-empty-projection','1')
      return ET.tostring(root,encoding='unicode')
     return value
    h.HAction.Run=action;h.GetTextFile=read
    with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))):r,e=own.run(request)
    self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(own.session._native_custody_held);self.assertTrue(deleted);self.assertTrue(stops);self.assertEqual(len(h.calls),stops[0]);self.assertNotIn(('Execute','InsertText'),[c[:2]for c in h.calls])
 def test_invalid_insert_result_after_proved_delete_is_first_fault_no_restore(self):
  for invalid in(False,1,None):
   with self.subTest(invalid=invalid):
    own,_,request=setup(self);h=own.hwp;execute=h.execute;hold=nt._Native.hold;events=[];faults=[]
    def insert(name,items):
     value=execute(name,items)
     return invalid if name=='InsertText'else value
    def latch(native,site,returned='invalid'):
     if site=='InsertText.Execute':faults.append(len(h.calls))
     return hold(native,site,returned)
    h.execute=insert
    with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))),patch.object(nt._Native,'hold',latch):r,e=own.run(request)
    self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(own.session._native_custody_held);self.assertTrue(faults);self.assertEqual(len(h.calls),faults[0]);self.assertEqual(sum(c[:2]==('Run','Delete')for c in h.calls),1)
 def test_target_only_empty_text_projection_never_normalizes_raw_or_nontext(self):
  own,_,_=setup(self);before=nt.parse(own.hwp.GetTextFile('HWPML2X',''));item=before['containers'][0];p=item['_rows'][0]['element'];root=ET.fromstring(own.hwp.GetTextFile('HWPML2X',''));target=next(x for x in root.iter('RECTANGLE'));char=next(target.iter('CHAR'));char.text='';empty=nt.parse(ET.tostring(root,encoding='unicode'));ep=empty['containers'][0]['_rows'][0]['element'];self.assertNotEqual(nt._signature(before),nt._signature(empty));self.assertEqual(nt._signature(before,mask=p),nt._signature(empty,mask=ep))
  for mode in('CHAR_attribute','P_attribute','foreign_head','sentinel_element'):
   with self.subTest(mode=mode):
    r=ET.fromstring(ET.tostring(root,encoding='unicode'));rect=next(r.iter('RECTANGLE'))
    if mode=='CHAR_attribute':next(rect.iter('CHAR')).set('projection-domain','complete-text')
    elif mode=='P_attribute':next(rect.iter('P')).set('qualified-runtime-caret-domain','1')
    elif mode=='foreign_head':r.find('HEAD').set('Projection','1')
    else:ET.SubElement(next(rect.iter('P')),'empty_run')
    changed=nt.parse(ET.tostring(r,encoding='unicode'));cp=changed['containers'][0]['_rows'][0]['element'];self.assertNotEqual(nt._signature(before,mask=p),nt._signature(changed,mask=cp))
