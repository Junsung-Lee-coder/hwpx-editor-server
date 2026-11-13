"""Independent constructor lifecycle SDK model: declared action/state contracts,
not production coordinate/census algorithm and not Box12 raw operands/native proof.
"""
import json,os,unittest
from pathlib import Path
from unittest.mock import patch
from app import native_text_containers as nt
from tests.native_text_containers_fakes import ContainersHwp,Ctrl
from tests.native_census_state_review_models import Control,link
from tests.test_native_constructor_postcreate import actual_owned,CREATE,native_root,owner
NATIVE='900000071'

class Lifecycle:
 def __init__(self,case,mode=4,*,sid='custody-session-0001'):
  self.own,self.trace,self.data=actual_owned(case,session_id=sid);self.h=self.own.hwp;self.action=self.h.HAction
  self.h.native_ids={owner(native_root()).find('SHAPEOBJECT').get('InstId'):NATIVE}
  self.mode=0;self.created_mode=mode;self.selected=None;self.cardinality=0;self.entered=False;self.events=[]
  self.h.HAction=self
 def GetDefault(self,name,hset):return self.action.GetDefault(name,hset)
 def Execute(self,name,hset):
  result=self.action.Execute(name,hset)
  if name=='DrawObjCreatorTextBox':
   self.mode=self.created_mode
   if self.mode in (4,20):self.selected=Ctrl('gso',NATIVE);self.cardinality=1
  return result
 def Run(self,name):
  self.h.calls.append(('Run',name))
  if name=='Cancel':
   assert self.mode in (4,20),'Only positively known object state may Cancel'
   self.mode=0;self.selected=None;self.cardinality=0;self.h.selection=None
  elif name=='SelectCtrlFront':
   assert self.mode==0 and self.selected is None and self.cardinality==0
   assert self.h.cursor==(0,0,238),'Independent actual fixture body owner anchor'
   self.mode=4;self.selected=Ctrl('gso',NATIVE);self.cardinality=1
  elif name=='ShapeObjTextBoxEdit':
   assert self.mode==4 and self.cardinality==1 and self.selected.GetCtrlInstID()==NATIVE
   self.mode=0;self.selected=None;self.cardinality=0;self.entered=True;self.h.selection=None;self.h.cursor=(2,0,0)
  else:raise AssertionError('Undeclared lifecycle action '+name)
  return True
 def head(self,_):
  nodes=[Control('secd',(0,0,0),'',self.events)]
  if self.data['created']:nodes.append(Control('gso',(0,0,238),NATIVE,self.events))
  return link(nodes)
 def patches(self):
  outer=self
  class Bundle:
   def __enter__(self):
    self.p=[patch.object(ContainersHwp,'SelectionMode',property(lambda h:outer.mode)),
     patch.object(ContainersHwp,'CurSelectedCtrl',property(lambda h:outer.selected),create=True),
     patch.object(ContainersHwp,'HeadCtrl',property(outer.head))]
    for p in self.p:p.start()
   def __exit__(self,*args):
    for p in reversed(self.p):p.stop()
  return Bundle()
 def retain(self,case,label,result):
  p=Path(os.environ['TMPDIR']).parent/'constructor-state-evidence';p.mkdir(exist_ok=True)
  (p/(case.id().split('.')[-1]+'-'+label+'.json')).write_text(json.dumps({'result':result,'calls':self.h.calls,'mode':self.mode,'entered':self.entered,
   'selected_cardinality_model':self.cardinality,'events':self.events,'held':self.own.session._native_custody_held},indent=1))

class ConstructorContract(unittest.TestCase):
 def test_known_object_and_strict_object_lifecycle_completes_requested_text(self):
  for mode in (4,20):
   with self.subTest(mode=mode):
    m=Lifecycle(self,mode)
    with m.patches():result,error=m.own.run(CREATE)
    m.retain(self,str(mode),result);self.assertIsNone(error);self.assertTrue(result['ok'],result)
    self.assertTrue(m.entered);self.assertFalse(m.own.session._native_custody_held)
    self.assertEqual([x[1] for x in m.h.calls if x[0]=='Run'],['Cancel','SelectCtrlFront','ShapeObjTextBoxEdit'])
    self.assertIn(CREATE['text'],''.join(m.data['root'].itertext()))
 def test_known_normal_constructor_uses_the_same_singleton_textentry_route(self):
  m=Lifecycle(self,0)
  with m.patches():result,error=m.own.run(CREATE)
  m.retain(self,'normal',result);self.assertIsNone(error);self.assertTrue(result['ok'],result);self.assertTrue(m.entered)
  self.assertEqual([x[1] for x in m.h.calls if x[0]=='Run'],['SelectCtrlFront','ShapeObjTextBoxEdit'])
 def test_unknown_mode_never_authorizes_cancel_or_text_entry(self):
  for mode in (True,None,5,16,32,36):
   with self.subTest(mode=mode):
    m=Lifecycle(self,mode)
    with m.patches():result,error=m.own.run(CREATE)
    m.retain(self,str(mode),result);self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(m.own.session._native_custody_held)
    self.assertEqual([x for x in m.h.calls if x[0]=='Run'],[])
    self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox']);self.assertTrue(result['dirty']);self.assertEqual(m.own.cleanup_events(),[])

 def test_column_table_modes_are_not_linear_text_or_constructor_completion(self):
  for mode in (2,3,17,18,19):
   with self.subTest(mode=mode):
    m=Lifecycle(self,mode)
    with m.patches():result,error=m.own.run(CREATE)
    m.retain(self,str(mode),result);self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(result['dirty'])
    self.assertTrue(m.own.session._native_custody_held);self.assertEqual([x for x in m.h.calls if x[0]=='Run'],[])

 def test_wrong_singleton_identity_and_locked_target_never_enter_or_insert(self):
  for fault in ('wrong_selected','wrong_constructor_selected','locked','latent_selection'):
   with self.subTest(fault=fault):
    m=Lifecycle(self,0 if fault=='latent_selection' else 4);original=m.Run;execute=m.Execute
    def run(name):
     value=original(name)
     if name=='SelectCtrlFront' and fault=='wrong_selected':m.selected=Ctrl('gso','900000072')
     return value
    def action(name,hset):
     value=execute(name,hset)
     if name=='DrawObjCreatorTextBox':
      if fault=='locked':owner(m.data['root']).find('SHAPEOBJECT').set('Lock','true')
      if fault=='latent_selection':m.selected=Ctrl('gso',NATIVE)
      if fault=='wrong_constructor_selected':m.selected=Ctrl('gso','900000072')
     return value
    m.Run=run;m.Execute=action
    with m.patches():result,error=m.own.run(CREATE)
    m.retain(self,fault,result);self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(m.own.session._native_custody_held)
    self.assertFalse(m.entered);self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox'])
    self.assertNotIn(('Run','ShapeObjTextBoxEdit'),m.h.calls)

 def test_transition_false_or_exception_has_no_postfault_sdk_or_cleanup(self):
  for name in ('Cancel','SelectCtrlFront','ShapeObjTextBoxEdit'):
   for failure in ('false','raises','int'):
    with self.subTest(name=name,failure=failure):
     m=Lifecycle(self);original=m.Run;stop=[]
     def run(action):
      value=original(action)
      if action==name:
       m.h.calls.append(('FIRST_TRANSITION_FAULT',name));stop.append(len(m.trace))
       if failure=='raises':raise RuntimeError('declared action fault')
       return False if failure=='false' else 1
      return value
     m.Run=run
     with m.patches():result,error=m.own.run(CREATE)
     m.retain(self,name+failure,result);self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(m.own.session._native_custody_held)
     self.assertTrue(stop);self.assertEqual(len(m.trace),stop[0]);self.assertEqual(m.h.calls[-1],('FIRST_TRANSITION_FAULT',name))
     self.assertEqual(m.own.cleanup_events(),[]);self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox'])

 def test_binding_purity_retains_raw_false_range_mode_bits_and_object_identity(self):
  from tests.test_native_constructor_postcreate import serial
  from app.native_numbering import NativeNumberingReadFailure
  for field in ('false_range','mode_bits','selected_identity'):
   with self.subTest(field=field):
    m=Lifecycle(self);m.data['created']=True;m.mode=0 if field=='false_range' else 4
    m.selected=None if field=='false_range' else Ctrl('gso',NATIVE);m.h.cursor=(0,0,246)
    model=nt.parse(serial(native_root()));item=model['containers'][0];changed=[];raw=[(False,0,0,0,0,0,0)]
    def selection():m.h.calls.append(('GetSelectedPos',));return raw[0]
    if field=='false_range':m.h.GetSelectedPos=selection
    def effect():
     changed.append(field)
     if field=='false_range':raw[0]=(False,0,0,1,0,0,1)
     elif field=='mode_bits':m.mode=20
     else:m.selected=Ctrl('gso','900000072')
    def head(_):return link([Control('secd',(0,0,0),'',m.events),Control('gso',(0,0,238),NATIVE,m.events,effect=effect)])
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)):
     native=nt._Native(m.h)
     with self.assertRaises(NativeNumberingReadFailure):nt._binding(native,model,item)
    self.assertEqual(changed,[field]);self.assertTrue(native.failed)
    self.assertEqual([x for x in m.h.calls if x[0] in ('Run','set_pos','Execute')],[])

 def test_strict_linear_mode_is_not_coerced_to_column_or_object(self):
  from app.native_numbering import NativeNumberingReadFailure
  m=Lifecycle(self);m.mode=17;m.h.selection=(0,0,0,0,0,1)
  with m.patches():
   native=nt._Native(m.h);error=None
   try:observed=native.state()
   except NativeNumberingReadFailure as exc:error=exc;observed=None
  self.assertIsNone(error);self.assertEqual(observed,((0,0,238),(0,0,0,0,0,1)))

 def test_insert_preparation_caret_drift_stops_before_execute(self):
  m=Lifecycle(self);original=m.GetDefault;drift=[]
  def default(name,hset):
   value=original(name,hset)
   if name=='InsertText':m.h.cursor=(0,0,238);drift.append(name)
   return value
  m.GetDefault=default
  with m.patches():result,error=m.own.run(CREATE)
  m.retain(self,'insert-drift',result);self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(m.own.session._native_custody_held)
  self.assertEqual(drift,['InsertText']);self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox'])
  self.assertEqual(m.own.cleanup_events(),[])
