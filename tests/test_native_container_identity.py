"""Native ID namespace/census contracts through real owning/service composition.
Desktop APIs are independently declared models, not real native qualification.
"""
import copy,json,os,unittest
from pathlib import Path
from types import MethodType,SimpleNamespace
from app import native_text_containers as nt
from app.local_cli_service import LocalCliService
from app.native_numbering import NativeNumberingReadFailure
from tests.native_text_containers_fakes import ContainersHwp,container,Ctrl
from tests.native_format_fakes import paragraph
from tests.test_native_numbering_runtime_custody import Owned
from tests.test_shapeobject_installed_accessor import StrictShape,RawTrace
from tests.test_native_constructor_postcreate import actual_owned,CREATE,native_root,owner

NATIVE='900000001'
def setup(case,kind='text_box',*,session_id='custody-session-0001',disjoint=True):
 hwp=ContainersHwp([paragraph('Start'),paragraph('lead',container(kind,text='Original container',ident='101')),paragraph('Tail')])
 hwp.native_ids={'101':NATIVE if disjoint else '101'};hwp._number_cells(hwp.body)
 # Equal-ID negative controls isolate the intended census fault from the
 # independently tested foreign-section defect; no target assertion is weakened.
 if not disjoint:hwp.owners[1]=None
 trace=[];hwp.HParameterSet.HShapeObject=StrictShape(trace,{},hwp.calls);hwp.hwp=RawTrace(hwp,trace)
 own=Owned(case,hwp,real_execute_live=True,session_id=session_id);hwp.cursor=(0,0,0)
 own.service._public_bundle_steps=MethodType(LocalCliService._public_bundle_steps,own.service)
 reply,error=own.run({'op':'text_container_inventory'});case.assertIsNone(error);case.assertTrue(reply['ok'],reply)
 item=reply['steps'][0]['result']['containers'][0]
 request={'op':'text_container_text_exact','target_id':item['target_id'],'expected_hash':item['hash'],'expected_text':item['text'],'new_text':'Updated container','confirm_layout':True}
 return own,trace,request

def keep(case,own,result):
 p=Path(os.environ['TMPDIR']).parent/'identity-evidence';p.mkdir(exist_ok=True)
 (p/(case.id().split('.')[-1]+'.json')).write_text(json.dumps({'test':case.id(),'reply':result,'calls':own.hwp.calls,
 'owning_HOLD':own.session._native_custody_held,'native_ids_independent_of_XML':getattr(own.hwp,'native_ids',{}),'scope':'SDK model, real owner/service/public path; no native proof'},indent=2)+'\n')
def held(case,own,result,error):
 case.assertIsNone(error);case.assertFalse(result['ok'],result);case.assertTrue(own.session._native_custody_held)
 case.assertEqual(own.hwp.executes(),[]);keep(case,own,result)

class IdentityContracts(unittest.TestCase):
 def test_foreign_section_does_not_enter_instance_getter(self):
  calls=[]
  class Section:
   CtrlID='secd'
   def GetCtrlInstID(self):calls.append('FORBIDDEN');return ''
  native=nt._Native(SimpleNamespace(hwp=SimpleNamespace(ParentCtrl=Section())))
  self.assertEqual(native.owner(),('secd',None));self.assertEqual(calls,[])
 def test_required_gso_and_note_IDs_stay_strict(self):
  for kind in ('gso','fn'):
   for value in ('123','',None,True,1,'bad'):
    with self.subTest(kind=kind,value=value):
     class Target:
      CtrlID=kind
      def GetCtrlInstID(self):return value
     native=nt._Native(SimpleNamespace(hwp=SimpleNamespace(ParentCtrl=Target())))
     if value=='123':self.assertEqual(native.owner(),(kind,value))
     else:
      with self.assertRaises(NativeNumberingReadFailure):native.owner()
      self.assertTrue(native.failed)
 def test_inventory_read_and_exact_edit_of_all_target_pairs_disjoint_namespaces(self):
  for kind in ('text_box','caption','footnote'):
   with self.subTest(kind=kind):
    own,trace,request=setup(self,kind)
    read,error=own.run({'op':'text_container_read','target_id':request['target_id'],'expected_hash':request['expected_hash']})
    self.assertIsNone(error);self.assertTrue(read['ok'],read)
    result,error=own.run(request);keep(self,own,result)
    self.assertIsNone(error);self.assertTrue(result['ok'],result);self.assertFalse(own.session._native_custody_held)
    self.assertEqual(own.hwp.cursor,(0,0,0))
 def test_native_constructor_create_all_the_way_with_disjoint_instance_ID(self):
  own,trace,state=actual_owned(self);xml_id=owner(native_root()).find('SHAPEOBJECT').get('InstId');own.hwp.native_ids={xml_id:NATIVE}
  result,error=own.run(CREATE);keep(self,own,result)
  self.assertIsNone(error);self.assertTrue(result['ok'],result);self.assertFalse(own.session._native_custody_held)
 def test_legacy_XML_equality_cannot_replace_a_required_fresh_native_binding(self):
  own,trace,request=setup(self,disjoint=False)
  def bad(nodes):
   for c in nodes:
    if c.kind=='gso':c.ident=''
   return nodes
  own.hwp.identity_controls=bad;result,error=own.run(request);held(self,own,result,error)
 def test_duplicate_native_IDs_and_foreign_anchors_hold_before_navigation_or_mutation(self):
  for kind in ('duplicate','foreign_anchor','case','wrongtype','empty_kind'):
   with self.subTest(kind=kind):
    own,trace,request=setup(self,disjoint=False)
    def bad(nodes):
     c=next(x for x in nodes if x.kind=='gso')
     if kind=='duplicate':nodes.append(copy.copy(c));nodes[-1].point=(0,2,0)
     elif kind=='foreign_anchor':c.point=(0,c.point[1],c.point[2]+1)
     elif kind=='case':c.kind='GSO'
     elif kind=='wrongtype':c.kind=True
     else:c.kind=''
     return nodes
    own.hwp.identity_controls=bad;result,error=own.run(request);held(self,own,result,error)
 def test_target_anchor_tuple_requires_exact_LONGs_not_truthy_coordinates(self):
  for value in (True,-1,2**31,'0'):
   with self.subTest(value=value):
    own,trace,request=setup(self,disjoint=False)
    def bad(nodes):
     next(c for c in nodes if c.kind=='gso').point=(0,1,value);return nodes
    own.hwp.identity_controls=bad;result,error=own.run(request);held(self,own,result,error)
 def test_census_identity_must_remain_fresh_across_navigation(self):
  own,trace,request=setup(self,disjoint=False);seen=[]
  def change(nodes):
   seen.append(1)
   if len(seen)>1:next(c for c in nodes if c.kind=='gso').ident=NATIVE
   return nodes
  own.hwp.identity_controls=change;result,error=own.run(request);held(self,own,result,error);self.assertGreater(len(seen),1)
 def test_required_parent_instance_empty_is_not_forgiven_by_valid_census(self):
  own,trace,request=setup(self,disjoint=False)
  for c in own.hwp.owners.values():
   if c is not None and c.CtrlID=='gso':c.ident=''
  result,error=own.run(request);held(self,own,result,error)
  self.assertEqual(result['steps'][0]['failure_diagnostic']['native_site'],'container.ParentCtrl.GetCtrlInstID')
 def test_parent_case_drift_is_technical_not_a_foreign_control_skip(self):
  own,trace,request=setup(self,disjoint=False)
  for c in own.hwp.owners.values():
   if c is not None and c.CtrlID=='gso':c.CtrlID='GSO'
  result,error=own.run(request);held(self,own,result,error)
