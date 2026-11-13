"""Finite CtrlID-only observations: real service/owner/Future with declared SDK.
Not Case13 raw operands/native acceptance. No behavior/normalization exemption.
"""
import hashlib,json,os,struct,unittest
from pathlib import Path
from unittest.mock import patch
from app import native_text_containers as nt,local_cli_runtime as rt
from tests.test_constructor_state_contract import Lifecycle,CREATE,NATIVE
from tests.native_census_state_review_models import Control,link
from tests.native_text_containers_fakes import ContainersHwp
from tests.test_native_container_identity import setup

class BadControl:
 def __init__(self,value,events,next=None,raise_value=None):self.value,self.events,self.next,self.raise_value=value,events,next,raise_value
 @property
 def CtrlID(self):
  self.events.append(('RETURN_CTRLID',))
  if self.raise_value is not None:raise self.raise_value
  return self.value
 @property
 def Next(self):self.events.append(('Next',));return self.next
 def GetAnchorPos(self,*a):self.events.append(('FORBIDDEN_ANCHOR',));raise AssertionError('foreign target getter')
 def GetCtrlInstID(self):self.events.append(('FORBIDDEN_ID',));raise AssertionError('foreign target getter')

def model(case,value,*,sid='custody-session-0001',foreign=False,raise_value=None):
 m=Lifecycle(case,4,sid=sid);events=[]
 m.pending_empty=type(value) is str and value=='' and raise_value is None
 def head(_):
  nodes=[Control('secd',(0,0,0),'',events),BadControl(value,events,raise_value=raise_value)]
  if foreign:nodes.append(Control('gso',(0,0,238),NATIVE,events))
  return link(nodes)
 return m,events,head

def retain(case,label,m,result,events):
 p=Path(os.environ['TMPDIR']).parent/'ctrlid-evidence';p.mkdir(exist_ok=True)
 (p/(case.id().split('.')[-1]+'-'+label+'.json')).write_text(json.dumps({'test':case.id(),'label':label,'reply':result,'calls':m.h.calls,'events':events,
  'owning_HOLD':m.own.session._native_custody_held,'cleanup':m.own.cleanup_events(),'scope':'Explicit SDK models; not historical CtrlID/native feature evidence'},indent=1))

def facts(case,m,result,error,events,label):
 retain(case,label,m,result,events);case.assertIsNone(error);case.assertFalse(result['ok'],result)
 case.assertTrue(m.own.session._native_custody_held);case.assertTrue(result['dirty'])
 case.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox'])
 case.assertEqual(m.own.cleanup_events(),[])
 d=result['steps'][0]['failure_diagnostic']
 if getattr(m,'pending_empty',False):
  case.assertEqual(events[-1],('Next',));case.assertEqual(d['native_site'],'container.binding.census');case.assertNotIn('native_ctrlid_boundary',d)
 else:
  case.assertEqual(events[-1],('RETURN_CTRLID',));case.assertEqual(d['native_site'],'container.control.CtrlID')
 case.assertEqual((d['causal_class'],d['cause_class']),('NativeNumberingMutationError','NativeNumberingReadFailure'))
 return d

class CtrlIDDiagnostic(unittest.TestCase):
 def run_value(self,value,label,*,raise_value=None):
  m,events,head=model(self,value,raise_value=raise_value);original=rt._native_diagnostic;published=[]
  def after_hold(*args):
   self.assertTrue(m.own.session._native_custody_held);published.append(True);return original(*args)
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(rt,'_native_diagnostic',after_hold):result,error=m.own.run(CREATE)
  d=facts(self,m,result,error,events,label);self.assertTrue(published);return m,d,result
 def test_exact_empty_and_case_values_survive_semantic_failure(self):
  for value in ('','GSO','GsO'):
   with self.subTest(label=value):
    m,d,result=self.run_value(value,value or 'empty')
    if value=='':
     # Empty census kind is pending, no longer immediate invalid telemetry.
     # Preserve the exact literal encoder oracle at the post-HOLD capsule seam.
     self.assertEqual(d['native_site'],'container.binding.census');self.assertNotIn('native_ctrlid_boundary',d)
     d=dict(zip(nt.CTRLID_DIAGNOSTIC_FIELDS,nt._project_ctrlid(nt._CtrlIDObservation('control.CtrlID','',True))))
    self.assertEqual(d.get('native_ctrlid_boundary'),'control.CtrlID');self.assertEqual(d.get('native_ctrlid_type'),'str')
    self.assertEqual(d.get('native_ctrlid_length'),str(len(value)));self.assertEqual(d.get('native_ctrlid_returned'),'true')
    self.assertEqual(d.get('native_ctrlid_primitive'),'utf16le_hex:'+value.encode('utf-16le','surrogatepass').hex())
    self.assertEqual(d.get('native_ctrlid_sha256'),'utf16le:'+hashlib.sha256(value.encode('utf-16le','surrogatepass')).hexdigest())
 def test_malformed_builtin_values_remain_strict_with_bounded_metadata(self):
  for value,label,kind,primitive in [(None,'none','none','none'),(True,'bool','bool','true'),(7,'int','int','int32:7'),(2**80,'large','int','omitted'),(1.5,'float','float','float64be:'+struct.pack('>d',1.5).hex()),(b'gso','bytes','bytes','bytes_hex:67736f')]:
   with self.subTest(label=label):
    m,d,result=self.run_value(value,label)
    self.assertEqual(d.get('native_ctrlid_type'),kind);self.assertEqual(d.get('native_ctrlid_primitive'),primitive)
 def test_opaque_and_string_subclasses_never_invoke_callbacks(self):
  events=[]
  class Meta(type):
   def __getattribute__(cls,name):events.append('typename/getattribute');raise AssertionError('type metadata forbidden')
   def __hash__(cls):events.append('typehash');raise AssertionError('typehash forbidden')
   def __eq__(cls,other):events.append('typeeq');raise AssertionError('typeeq forbidden')
  class Opaque(metaclass=Meta):
   def __repr__(self):events.append('repr');raise AssertionError('repr forbidden')
   def __str__(self):events.append('str');raise AssertionError('str forbidden')
   def __bool__(self):events.append('bool');raise AssertionError('bool forbidden')
   def __len__(self):events.append('len');raise AssertionError('len forbidden')
   def __getattribute__(self,key):events.append('attr');raise AssertionError('attribute forbidden')
   def __eq__(self,x):events.append('eq');raise AssertionError('eq forbidden')
  class Text(str):
   def __len__(self):events.append('subclasslen');raise AssertionError('subclass forbidden')
   def __str__(self):events.append('subclassstr');raise AssertionError('subclass forbidden')
  for label,value in [('opaque',Opaque()),('str_subclass',Text('gso'))]:
   with self.subTest(label=label):
    m,d,result=self.run_value(value,label);self.assertEqual(d.get('native_ctrlid_type'),'opaque')
    self.assertEqual(d.get('native_ctrlid_length'),'NA');self.assertEqual(d.get('native_ctrlid_primitive'),'omitted');self.assertEqual(events,[])
 def test_getter_exception_records_reached_boundary_not_invented_value(self):
  m,d,result=self.run_value(None,'getter-raised',raise_value=RuntimeError('PRIVATE-getter-error'))
  self.assertEqual(d.get('native_ctrlid_boundary'),'control.CtrlID');self.assertEqual(d.get('native_ctrlid_type'),'unreturned')
  self.assertEqual(d.get('native_ctrlid_returned'),'false');self.assertEqual(d.get('native_ctrlid_primitive'),'omitted')
  self.assertNotIn('PRIVATE-',json.dumps(result))
 def test_foreign_success_and_padded_target_refusal_are_not_normalized(self):
  for value in ('secd','tbl'):
   with self.subTest(value=value):
    m,events,head=model(self,value,foreign=True)
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)):result,error=m.own.run(CREATE)
    retain(self,value,m,result,events);self.assertIsNone(error);self.assertTrue(result['ok']);self.assertFalse(m.own.session._native_custody_held)
    self.assertNotIn(('FORBIDDEN_ANCHOR',),events);self.assertNotIn(('FORBIDDEN_ID',),events);self.assertNotIn('native_ctrlid_',json.dumps(result))
  m,events,head=model(self,'gso ')
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)):result,error=m.own.run(CREATE)
  retain(self,'padded',m,result,events);self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(m.own.session._native_custody_held)
  self.assertEqual(result['steps'][0]['failure_diagnostic']['native_site'],'container.binding.census')
  self.assertNotIn('native_ctrlid_boundary',result['steps'][0]['failure_diagnostic'],'unrelated last observation is not a failing operand')
 def test_ParentCtrl_and_selected_kind_failures_keep_the_exact_reached_site(self):
  own,trace,request=setup(self);events=[];bad=BadControl('GSO',events);old=ContainersHwp.ParentCtrl
  with patch.object(ContainersHwp,'ParentCtrl',property(lambda h:bad if h.cursor[0]==1 else old.fget(h))):result,error=own.run(request)
  self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(own.session._native_custody_held)
  d=result['steps'][0]['failure_diagnostic'];self.assertEqual(d.get('native_ctrlid_boundary'),'ParentCtrl.CtrlID');self.assertEqual(d.get('native_ctrlid_type'),'str');self.assertEqual(own.hwp.executes(),[])
  m=Lifecycle(self);execute=m.Execute;events=[]
  def action(name,hset):
   ret=execute(name,hset)
   if name=='DrawObjCreatorTextBox':m.selected=BadControl('GSO',events)
   return ret
  m.Execute=action
  with m.patches():result,error=m.own.run(CREATE)
  d=result['steps'][0]['failure_diagnostic'];self.assertFalse(result['ok']);self.assertEqual(d['native_site'],'container.selected.identity');self.assertEqual(d.get('native_ctrlid_boundary'),'selected.CtrlID')
 def test_unreached_selected_CtrlID_never_reuses_a_previous_observation(self):
  m=Lifecycle(self);execute=m.Execute
  def action(name,hset):
   value=execute(name,hset)
   if name=='DrawObjCreatorTextBox':m.selected=None
   return value
  m.Execute=action
  with m.patches():result,error=m.own.run(CREATE)
  d=result['steps'][0]['failure_diagnostic'];self.assertFalse(result['ok']);self.assertEqual(d['native_site'],'container.selected.identity');self.assertNotIn('native_ctrlid_boundary',d)
 def test_diagnostic_publication_failure_keeps_original_HOLD_cause_and_Future(self):
  m,events,head=model(self,'GSO');called=[]
  def failure(*args):called.append(m.own.session._native_custody_held);raise MemoryError('diagnostic only')
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_project_ctrlid',failure,create=True):result,error=m.own.run(CREATE)
  d=facts(self,m,result,error,events,'publication-failed');self.assertEqual(called,[True,True]);self.assertEqual(d['native_return'],'invalid')
 def test_prevalidation_capture_failure_is_optional_and_not_an_acceptance_change(self):
  m,events,head=model(self,'GSO');called=[]
  def failed(*a,**k):called.append(True);raise MemoryError('capture only')
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_CtrlIDObservation',failed,create=True):result,error=m.own.run(CREATE)
  d=facts(self,m,result,error,events,'capture-failed');self.assertTrue(called);self.assertEqual(d['native_return'],'invalid')

 def test_foreign_error_storage_key_cannot_run_equality_during_capture_attach(self):
  m,events,head=model(self,'GSO');callbacks=[];original=nt._read_failure
  class Key:
   def __hash__(self):return hash('_container_ctrlid_observation')
   def __eq__(self,other):callbacks.append('foreign-key-eq');raise AssertionError('no foreign key callback')
  def error(*args):
   exc=original(*args);data=BaseException.__dict__['__dict__'].__get__(exc);dict.__setitem__(data,Key(),None);return exc
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_read_failure',error):result,escaped=m.own.run(CREATE)
  d=facts(self,m,result,escaped,events,'foreign-error-key');self.assertEqual(callbacks,[]);self.assertEqual(d['native_return'],'invalid')
