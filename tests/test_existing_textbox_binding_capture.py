"""Legacy append diagnostic producer: STRUCTURAL SDK FAKE, NOT native25 HML.
The corrected product does not append old+new. These tests explicitly use the
retired-consumer model while the actual diagnostic methods/runtime stay real.
Authentic pair evidence is separately private; raw failed SDK return is unknown.
"""
import hashlib,json,unittest
from unittest.mock import patch
from app import native_text_containers as nt,local_cli_runtime as rt
from tests.test_native_container_identity import setup,NATIVE
from tests.native_census_state_review_models import Control,link
from tests.test_empty_ctrlid_product import Empty
from tests.native_text_containers_fakes import ContainersHwp
from tests.legacy_append_diagnostic_model import legacy_append_model
SECRET='SYNTHETIC-PRIVATE-PAIR-NOT-NATIVE25'
KEY='container_binding_precomparison'
def last(own):return max(own.session._commands_by_id.values(),key=lambda c:c.sequence)
class SameCommandCapture(unittest.TestCase):
 def scenario(self,foreign=False,write_fault=False):
  own,_,request=setup(self);h=own.hwp;old=h.execute;events=[];writes=[];faults=[];latches=[];persist=rt.LocalCliLiveSession._persist_command;hold=nt._Native.hold
  def execute(name,items):
   value=old(name,items)
   if name=='InsertText'and foreign:h.body[0]['atoms'].append({'cs':h.body[0]['atoms'][-1]['cs'],'c':SECRET})
   return value
  def writer(session,command):
   p=command.reconciliation_data.get(KEY)
   if p is not None and not writes:
    writes.append({'held':session._native_custody_held,'native_calls':len(h.calls),'record':dict(p)})
    if write_fault:faults.append(True);raise OSError('synthetic private journal unavailable')
   return persist(session,command)
  def latch(native,site,returned='invalid'):
   if site=='binding.document':latches.append(len(h.calls))
   return hold(native,site,returned)
  h.execute=execute
  with patch.object(nt,'execute',legacy_append_model),patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))),patch.object(rt.LocalCliLiveSession,'_persist_command',writer),patch.object(nt._Native,'hold',latch):r,e=own.run(request)
  return own,h,r,e,writes,faults,latches
 def test_precomparison_pair_is_durable_before_mismatch_latch(self):
  own,h,r,e,writes,_,latches=self.scenario(foreign=True);self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(own.session._native_custody_held);self.assertTrue(latches);self.assertEqual(len(h.calls),latches[0]);self.assertNotIn(('Run','Delete'),h.calls)
  self.assertEqual(len(writes),1,'same-command precomparison evidence missing before source binding refusal');w=writes[0];self.assertFalse(w['held']);p=w['record'];self.assertEqual(p['schema'],'container-binding-precomparison/v1');self.assertEqual(p['phase'],'owner.selection_identity');self.assertNotIn(SECRET,p['expected_hml']);self.assertIn(SECRET,p['observed_hml']);self.assertIs(p['insert_execute_raw'],True);self.assertIs(p['insert_execute_returned'],True)
  for role in('expected','observed'):self.assertEqual(p[role+'_hml_sha256_utf16le'],hashlib.sha256(p[role+'_hml'].encode('utf-16-le',errors='surrogatepass')).hexdigest())
  disk=json.loads(rt.command_journal_path(own.root).read_text());self.assertTrue(any(c.get('reconciliation_data',{}).get(KEY)==p for c in disk['commands']));self.assertNotIn(SECRET,json.dumps(r));self.assertNotIn('"expected_hml":',json.dumps(r));self.assertEqual(r['steps'][0]['failure_diagnostic']['native_binding_branch'],'append_signature')
 def test_healthy_edit_retains_private_pair_without_changing_result(self):
  own,h,r,e,writes,_,latches=self.scenario();self.assertIsNone(e);self.assertTrue(r['ok']);self.assertFalse(own.session._native_custody_held);self.assertFalse(latches);self.assertEqual(len(writes),1);self.assertFalse(writes[0]['held']);self.assertIn(('Run','Delete'),h.calls);self.assertNotIn(KEY,json.dumps(r))
 def test_optional_write_failure_keeps_original_refusal_and_no_next_SDK(self):
  own,h,r,e,writes,faults,latches=self.scenario(foreign=True,write_fault=True);self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(own.session._native_custody_held);self.assertTrue(r['dirty']);self.assertEqual(faults,[True]);self.assertEqual(len(h.calls),latches[0]);self.assertEqual(r['steps'][0]['failure_diagnostic']['native_site'],'container.binding.document');self.assertNotIn('synthetic private journal unavailable',json.dumps(r))
 def test_capture_write_failure_is_optional_on_healthy_path(self):
  own,h,r,e,writes,faults,_=self.scenario(write_fault=True);self.assertIsNone(e);self.assertTrue(r['ok']);self.assertFalse(own.session._native_custody_held);self.assertEqual(faults,[True]);self.assertIn(('Run','Delete'),h.calls)
 def test_bounded_typed_private_writer_and_occupied_slots_are_inert(self):
  own,_,_=setup(self);h=own.hwp;callbacks=[];seen=[]
  class Opaque:
   def __str__(self):callbacks.append('str');raise AssertionError
   def __len__(self):callbacks.append('len');raise AssertionError
   def __eq__(self,other):callbacks.append('eq');raise AssertionError
   def __hash__(self):return 41
  class Text(str):
   def __len__(self):callbacks.append('subclass-len');raise AssertionError
  good=('owner.selection_identity','<HWPML/>','<HWPML/>','text_box:101',True)
  bad=[None,{},('bad',*good[1:]),(good[0],Opaque(),*good[2:]),(good[0],Text('x'),*good[2:]),(good[0],'x'*65537,*good[2:]),(*good[:3],Opaque(),True),(*good[:4],1)]
  def observe(handle):
   c=own.session._active_command;before_calls=len(h.calls)
   for item in bad:handle.record_binding_precomparison(item);self.assertNotIn(KEY,c.reconciliation_data)
   original=c.reconciliation_data
   for occupied in({KEY:'keep'}, {'container_binding_precomparison_write_returned':False},{Opaque():0}):
    c.reconciliation_data=occupied;handle.record_binding_precomparison(good);self.assertIs(c.reconciliation_data,occupied)
   c.reconciliation_data=original;handle.record_binding_precomparison(good);p=c.reconciliation_data[KEY];handle.record_binding_precomparison((good[0],'<different/>',*good[2:]));self.assertIs(c.reconciliation_data[KEY],p);self.assertTrue(c.reconciliation_data['container_binding_precomparison_write_returned']);self.assertEqual(len(h.calls),before_calls);seen.append(True);return {'ok':True,'dirty':False}
  own.session.execute('command-bundle',observe);self.assertEqual(seen,[True]);self.assertEqual(callbacks,[])
 def test_invalid_native_InsertText_result_keeps_first_fault_without_capture(self):
  own,_,request=setup(self);h=own.hwp;old=h.execute;events=[];faults=[];hold=nt._Native.hold
  def execute(name,items):
   result=old(name,items)
   return 1 if name=='InsertText'else result
  def latch(native,site,returned='invalid'):
   if site=='InsertText.Execute':faults.append(len(h.calls))
   return hold(native,site,returned)
  h.execute=execute
  with patch.object(nt,'execute',legacy_append_model),patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))),patch.object(nt._Native,'hold',latch):r,e=own.run(request)
  self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(own.session._native_custody_held);self.assertTrue(faults);self.assertEqual(len(h.calls),faults[0]);self.assertNotIn(KEY,last(own).reconciliation_data);self.assertNotIn(('Run','Delete'),h.calls)
