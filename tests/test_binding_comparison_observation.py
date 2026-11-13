"""Exact existing-comparison operands, private journal only; no native cause claim."""
import json,os,unittest
from pathlib import Path
from unittest.mock import patch
from app import native_text_containers as nt,local_cli_runtime as rt
from tests.test_constructor_state_contract import Lifecycle,CREATE
from tests.test_empty_ctrlid_product import create_head,ContainersHwp
from tests.test_native_container_identity import setup,NATIVE
from tests.native_census_state_review_models import Control,link
from tests.test_empty_ctrlid_product import Empty
SECRET='PRIVATE-HML-OPERAND-NOT-FOR-PUBLIC'

def mismatch(case,sid='custody-session-0001'):
 m=Lifecycle(case,sid=sid);events=[];fired=[]
 def change():
  if not fired:m.data['root'].find('HEAD').set('ComparisonPrivate',SECRET);fired.append(True)
 return m,events,create_head(m,events,effect=change)

def command(m):return max(m.own.session._commands_by_id.values(),key=lambda c:c.sequence)
def private(m):return command(m).reconciliation_data.get('container_binding_comparison')
def facts(case,m,r,e):
 case.assertIsNone(e);case.assertFalse(r['ok']);case.assertTrue(r['dirty']);case.assertTrue(m.own.session._native_custody_held)
 case.assertEqual(m.own.cleanup_events(),[]);case.assertEqual([c[1] for c in m.h.executes()],['DrawObjCreatorTextBox'])
 d=r['steps'][0]['failure_diagnostic'];case.assertEqual(d['native_site'],'container.binding.document');case.assertEqual(d['native_return'],'invalid')
 case.assertEqual((d['causal_class'],d['cause_class']),('NativeNumberingMutationError','NativeNumberingReadFailure'));return d

def retain(case,name,m,r):
 p=Path(os.environ['TMPDIR']).parent/'comparison-evidence';p.mkdir(exist_ok=True)
 (p/(case.id().split('.')[-1]+'-'+name+'.json')).write_text(json.dumps({'result':r,'calls':m.h.calls,'private':private(m),'scope':'Owned fixture already-read HML; declared SDK only, NOT native proof.'},ensure_ascii=True))

class BindingComparisonObservation(unittest.TestCase):
 def test_full_mismatch_retains_exact_pair_durable_private_and_no_public_raw_documents(self):
  m,events,head=mismatch(self)
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)):r,e=m.own.run(CREATE)
  d=facts(self,m,r,e);p=private(m);self.assertIs(type(p),dict);self.assertEqual(p['branch'],'full_signature');self.assertEqual(p['phase'],'constructor.initial')
  self.assertNotIn(SECRET,p['expected_hml']);self.assertIn(SECRET,p['observed_hml']);self.assertNotEqual(p['expected_hml'],p['observed_hml'])
  self.assertEqual(p['expected_signature'],nt._signature(nt.parse(p['expected_hml'])));self.assertEqual(p['observed_signature'],nt._signature(nt.parse(p['observed_hml'])))
  self.assertEqual(d['native_binding_expected'],'logical_sha256:'+p['expected_signature']);self.assertEqual(d['native_binding_observed'],'logical_sha256:'+p['observed_signature'])
  disk=json.loads(rt.command_journal_path(m.own.root).read_text());self.assertTrue(any(x.get('reconciliation_data',{}).get('container_binding_comparison')==p for x in disk['commands']))
  self.assertNotIn(SECRET,json.dumps(r));self.assertNotIn('expected_hml',json.dumps(m.own.session.command_status()));self.assertNotIn('"observed_hml":',json.dumps(r));retain(self,'full',m,r)
 def test_healthy_native_authored_Create_keeps_no_comparison_payload_or_new_HOLD(self):
  m=Lifecycle(self)
  with m.patches():r,e=m.own.run(CREATE)
  self.assertIsNone(e);self.assertTrue(r['ok']);self.assertFalse(m.own.session._native_custody_held);self.assertIsNone(private(m));self.assertNotIn('native_binding_',json.dumps(r))
 def test_append_text_mismatch_reports_actual_request_bound_operands_without_broadening(self):
  own,_,request=setup(self);h=own.hwp;events=[];old=h.execute
  def execute(name,items):
   ret=old(name,items)
   if name=='InsertText':
    p=h.lists[h.cursor[0]][h.cursor[1]];p['atoms'].append({'cs':p['atoms'][-1]['cs'],'c':'!'})
   return ret
  h.execute=execute
  with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))):r,e=own.run(request)
  self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(own.session._native_custody_held);self.assertNotIn(('Run','Delete'),h.calls)
  c=max(own.session._commands_by_id.values(),key=lambda c:c.sequence);p=c.reconciliation_data.get('container_binding_comparison');self.assertIs(type(p),dict);self.assertEqual(p['branch'],'append_text')
  self.assertEqual(p['expected_text'],'Original container'+request['new_text']);self.assertEqual(p['observed_text'],p['expected_text']+'!');self.assertEqual(own.cleanup_events(),[])
 def test_capture_allocation_failure_is_optional_and_preserves_original_cause(self):
  m,events,head=mismatch(self)
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_make_binding_observation',side_effect=MemoryError('capture'),create=True):r,e=m.own.run(CREATE)
  facts(self,m,r,e);self.assertIsNone(private(m))
 def test_cache_entrypoint_failure_cannot_replace_the_already_detected_first_fault(self):
  m,events,head=mismatch(self)
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_cache_binding',side_effect=MemoryError('optional cache entry')):r,e=m.own.run(CREATE)
  facts(self,m,r,e);self.assertIsNone(private(m))
 def test_public_projection_failure_stays_post_HOLD_private_pair_still_available(self):
  m,events,head=mismatch(self);held=[]
  def fail(*args):held.append(m.own.session._native_custody_held);raise MemoryError('publication')
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_project_binding',fail,create=True):r,e=m.own.run(CREATE)
  facts(self,m,r,e);self.assertTrue(held);self.assertTrue(all(held));self.assertIn(SECRET,private(m)['observed_hml'])
 def test_private_journal_publication_failure_cannot_replace_first_fault_or_unsettle_Future(self):
  m,events,head=mismatch(self);old=m.own.session._persist_command;held=[]
  def persist(c):
   if 'container_binding_comparison' in c.reconciliation_data and not held:held.append(m.own.session._native_custody_held);raise OSError('private journal refused')
   return old(c)
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(m.own.session,'_persist_command',persist):r,e=m.own.run(CREATE)
  facts(self,m,r,e);self.assertTrue(held);self.assertTrue(all(held));self.assertTrue(command(m).completed_event.is_set())
 def test_comparison_exception_keeps_first_cause_and_exact_available_operands(self):
  m,events,head=mismatch(self);old=nt._signature;first=ValueError('original comparison');calls=[]
  def signature(model,**kw):
   if model['root'].find('HEAD').get('ComparisonPrivate')==SECRET:calls.append(len(m.h.calls));raise first
   return old(model,**kw)
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_signature',signature):r,e=m.own.run(CREATE)
  self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(m.own.session._native_custody_held);self.assertTrue(calls);self.assertEqual(m.h.calls[calls[0]:],[])
  p=private(m);self.assertEqual(p['branch'],'comparison_raised');self.assertIn(SECRET,p['observed_hml']);self.assertEqual(r['steps'][0]['failure_diagnostic']['native_return'],'raised');self.assertEqual(m.own.cleanup_events(),[])
  self.assertIs(command(m).error.__cause__.__cause__.__cause__,first)
 def test_append_signature_preserves_foreign_delta_and_optional_digest_failure_is_post_HOLD(self):
  for fault in ('foreign','digest'):
   with self.subTest(fault=fault):
    own,_,request=setup(self);h=own.hwp;events=[];old=h.execute;project=nt._project_binding;held=[]
    def execute(name,items):
     ret=old(name,items)
     if name=='InsertText':
      p=h.body[0] if fault=='foreign' else h.lists[h.cursor[0]][h.cursor[1]];p['atoms'].append({'cs':p['atoms'][-1]['cs'],'c':'!'})
     return ret
    def fail_digest(*args):held.append(own.session._native_custody_held);raise MemoryError('optional digest')
    def publication(cap):
     if fault=='digest':
      with patch.object(nt.hashlib,'sha256',fail_digest):return project(cap)
     return project(cap)
    h.execute=execute
    with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))),patch.object(nt,'_project_binding',publication):r,e=own.run(request)
    self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(own.session._native_custody_held);self.assertEqual(own.cleanup_events(),[])
    c=max(own.session._commands_by_id.values(),key=lambda c:c.sequence);p=c.reconciliation_data['container_binding_comparison'];self.assertEqual(p['branch'],'append_signature' if fault=='foreign' else 'append_text')
    self.assertNotIn(('Run','Delete'),h.calls)
    if fault=='digest':self.assertTrue(held);self.assertTrue(all(held))
 def test_missing_append_peer_retains_count_and_does_not_hash_an_unread_operand(self):
  from xml.etree import ElementTree as E
  own,_,request=setup(self);h=own.hwp;events=[];original=h.GetTextFile
  def source(fmt,option):
   text=original(fmt,option)
   if any(c[:2]==('Execute','InsertText') for c in h.calls):
    root=E.fromstring(text)
    for parent in root.iter():
     for child in list(parent):
      if child.tag=='DRAWTEXT':parent.remove(child)
    return E.tostring(root,encoding='unicode')
   return text
  h.GetTextFile=source
  with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),NATIVE,events)]))):r,e=own.run(request)
  self.assertIsNone(e);self.assertFalse(r['ok']);self.assertTrue(own.session._native_custody_held);self.assertEqual(own.cleanup_events(),[])
  c=max(own.session._commands_by_id.values(),key=lambda c:c.sequence);p=c.reconciliation_data['container_binding_comparison'];self.assertEqual(p['branch'],'append_peer_count');self.assertEqual(p['observed_peer_count'],0)
  self.assertIsNone(p['observed_signature']);self.assertIsNone(p['observed_text']);self.assertNotIn(('Run','Delete'),h.calls)
 def test_opaque_cached_telemetry_never_calls_objects_or_type_metadata(self):
  calls=[]
  class Meta(type):
   def __getattribute__(cls,key):calls.append('typename');raise AssertionError()
   def __eq__(cls,other):calls.append('typeeq');raise AssertionError()
   def __hash__(cls):calls.append('typehash');raise AssertionError()
  class Poison(metaclass=Meta):
   def __repr__(self):calls.append('repr');raise AssertionError()
   def __str__(self):calls.append('str');raise AssertionError()
   def __bool__(self):calls.append('bool');raise AssertionError()
   def __getattribute__(self,key):calls.append('attr');raise AssertionError()
   def __eq__(self,other):calls.append('eq');raise AssertionError()
  for cap in (Poison(),tuple(Poison() for _ in range(10))):
   error=nt._read_failure('fixed','binding.document','invalid');dict.__setitem__(BaseException.__dict__['__dict__'].__get__(error),'_container_binding_observation',cap)
   d=rt._native_diagnostic('bundle_step',error);self.assertNotIn('native_binding_branch',d)
  self.assertEqual(calls,[])
