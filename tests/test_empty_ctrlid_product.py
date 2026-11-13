"""Prospective closed-census product proof. No claim about box14 topology/native cause."""
import copy,json,os,unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as E
from app import native_text_containers as nt
from tests.test_constructor_state_contract import Lifecycle,CREATE,NATIVE
from tests.native_census_state_review_models import Control,link
from tests.native_text_containers_fakes import ContainersHwp,container
from tests.native_format_fakes import paragraph
from tests.test_native_container_identity import setup,NATIVE as EDIT_ID

class Empty(Control):
 def __init__(self,events,effect=None):super().__init__('',None,None,events);self.next_effect=effect
 def __bool__(self):raise AssertionError('Empty CtrlID is not node truthiness/terminal')
 @property
 def Next(self):
  self.events.append(('Next',''))
  if self.next_effect is not None:self.next_effect();self.next_effect=None
  return self.next

def create_head(m,events,where=1,*,omit=False,kind='gso',point=(0,0,238),ident=NATIVE,effect=None,extra=None):
 def head(_):
  nodes=[Control('secd',(0,0,0),'',events)]
  if not omit:nodes.append(Control(kind,point,ident,events))
  if extra:nodes.extend(extra(events))
  nodes.insert(min(where,len(nodes)),Empty(events,effect));return link(nodes)
 return head

def record(case,label,m,result,events):
 p=Path(os.environ['TMPDIR']).parent/'empty-census-evidence';p.mkdir(exist_ok=True)
 (p/(case.id().split('.')[-1]+'-'+label+'.json')).write_text(json.dumps({'test':case.id(),'label':label,'result':result,'events':events,'calls':m.h.calls,'held':m.own.session._native_custody_held,'cleanup':m.own.cleanup_events(),'scope':'Portable declared SDK/native-authored-format proof, NOT native acceptance.'},default=str,ensure_ascii=True))

class EmptyCensusProduct(unittest.TestCase):
 def run_create(self,head_args,*,effect=None):
  m=Lifecycle(self);events=[]
  if effect is not None:head_args={**head_args,'effect':lambda:effect(m)}
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events,**head_args))):result,error=m.own.run(CREATE)
  record(self,str(head_args.get('where',1)),m,result,events);self.assertIsNone(error);self.assertEqual(m.own.cleanup_events(),[])
  return m,result,events
 def test_native_authored_constructor_empty_first_middle_terminal_still_enters_and_inserts(self):
  for where in (0,1,2):
   with self.subTest(where=where):
    m,r,e=self.run_create({'where':where});self.assertTrue(r['ok'],r);self.assertTrue(m.entered)
    self.assertIn(CREATE['text'],''.join(m.data['root'].itertext()));self.assertFalse(m.own.session._native_custody_held)
    self.assertIn(('Next',''),e);self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox','InsertText'])
 def test_empty_actual_required_target_cannot_substitute_and_has_no_text_edit_or_cleanup(self):
  m,r,e=self.run_create({'omit':True});self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(m.own.session._native_custody_held)
  self.assertEqual(r['steps'][0]['failure_diagnostic']['native_site'],'container.binding.census');self.assertIn(('Next',''),e)
  self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox']);self.assertFalse(m.entered)
 def test_malformed_case_padded_missing_duplicate_wronganchor_and_ID_stay_refused(self):
  cases=[{'kind':None},{'kind':True},{'kind':'GSO'},{'kind':'gso '},{'point':(0,0,239)},{'ident':''},
   {'extra':lambda e:[Control('gso',(0,0,238),NATIVE,e)]},{'extra':lambda e:[Control('gso',(7,0,0),'900000099',e)]},
   {'extra':lambda e:[Control('fn',(8,0,0),'900000098',e)]}]
  for case in cases:
   with self.subTest(case=str(case)):
    m,r,e=self.run_create(case);self.assertFalse(r['ok'],r);self.assertTrue(m.own.session._native_custody_held)
    self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox']);self.assertFalse(m.entered)
 def test_complete_foreign_nodes_and_required_pairs_for_all_three_edit_families(self):
  for family in ('text_box','caption','footnote'):
   with self.subTest(family=family):
    own,_,request=setup(self,family);events=[];kind='fn' if family=='footnote' else 'gso'
    def head(_):return link([Empty(events),Control('tbl',(9,0,0),'',events),Control(kind,(0,1,4),EDIT_ID,events),Empty(events)])
    with patch.object(ContainersHwp,'HeadCtrl',property(head)):r,error=own.run(request)
    self.assertIsNone(error);self.assertTrue(r['ok'],r);self.assertFalse(own.session._native_custody_held)
    self.assertEqual(own.cleanup_events(),[]);self.assertIn(('Next',''),events)
 def test_rich_multiple_same_kind_targets_match_all_anchors_and_disjoint_IDs(self):
  own,_,request=setup(self);h=own.hwp;h.body.append(paragraph('xx',container('caption',ident='202',text='Untouched caption')));h.native_ids['202']='900000002';h._number_cells(h.body);events=[]
  rich_model=nt.parse(h.GetTextFile('HWPML2X',''));request['expected_hash']=rich_model['containers'][0]['hash']
  self.assertEqual(set(nt._body_control_anchors(rich_model)),{('gso',(0,1,4)),('gso',(0,3,2))},'Manual fixture anchor declarations must be qualified independently before product call')
  def head(_):return link([Control('secd',(0,0,0),'',events),Empty(events),Control('gso',(0,1,4),EDIT_ID,events),Control('gso',(0,3,2),'900000002',events),Empty(events)])
  with patch.object(ContainersHwp,'HeadCtrl',property(head)):r,error=own.run(request)
  self.assertIsNone(error);self.assertTrue(r['ok'],r);self.assertEqual(own.cleanup_events(),[])
  self.assertIn('Untouched caption',h.GetTextFile('HWPML2X',''))
 def test_foreign_serialized_damage_is_not_omitted_by_empty_classification(self):
  fired=[]
  def damage(m):
   if not fired:m.data['root'].find('HEAD').set('ForeignDamage','1');fired.append(True)
  m,r,e=self.run_create({},effect=damage);self.assertFalse(r['ok'],r);self.assertTrue(m.own.session._native_custody_held)
  self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox']);self.assertFalse(m.entered)
 def test_census_witness_drift_latches_before_added_document_read(self):
  counts=[]
  def drift(m):
   if not counts:m.h.cursor=(0,7,0);counts.append(sum(c[0]=='GetTextFile' for c in m.h.calls))
  m,r,e=self.run_create({},effect=drift);self.assertFalse(r['ok']);self.assertTrue(m.own.session._native_custody_held)
  self.assertTrue(counts,'A valid prospective empty node must reach Next before the purity failure');self.assertEqual(sum(c[0]=='GetTextFile' for c in m.h.calls),counts[0]);self.assertFalse(m.entered)
 def test_empty_ParentCtrl_and_selected_required_kind_still_hold_and_publish_empty(self):
  from tests.test_ctrlid_observation import BadControl
  for site in ('selected','parent'):
   with self.subTest(site=site):
    m=Lifecycle(self);events=[];old=m.Execute;parent=ContainersHwp.ParentCtrl
    def execute(name,pset):
     ret=old(name,pset)
     if name=='DrawObjCreatorTextBox' and site=='selected':m.selected=BadControl('',events)
     return ret
    m.Execute=execute
    prop=property(lambda h:BadControl('',events) if h.cursor[0]>0 else parent.fget(h))
    with m.patches(),patch.object(ContainersHwp,'ParentCtrl',prop):r,error=m.own.run(CREATE)
    self.assertIsNone(error);self.assertFalse(r['ok']);self.assertTrue(m.own.session._native_custody_held);self.assertEqual(m.own.cleanup_events(),[])
    d=r['steps'][0]['failure_diagnostic'];self.assertEqual(d['native_ctrlid_length'],'0');self.assertEqual(d['native_ctrlid_primitive'],'utf16le_hex:');self.assertEqual(d['native_ctrlid_returned'],'true')
    self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox'])
 def test_mixed_or_nested_required_HML_owners_are_not_hidden_by_empty_nodes(self):
  for form in ('mixed','nested'):
   with self.subTest(form=form):
    own,_,_=setup(self);h=own.hwp;model=nt.parse(h.GetTextFile('HWPML2X',''));root=model['root'];item=model['containers'][0]
    if form=='mixed':E.SubElement(item['_owner'],'FOOTNOTE')
    else:
     extra=copy.deepcopy(item['_owner']);extra.find('SHAPEOBJECT').set('InstId','202');item['_owner'].append(extra)
    model=nt.parse(E.tostring(root,encoding='unicode'));item=model['containers'][0];events=[]
    native=nt._Native(h)
    with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),EDIT_ID,events)]))):
     with self.assertRaises(Exception):nt._binding(native,model,item)
    self.assertTrue(native.failed);self.assertEqual(h.executes(),[])
 def test_intermediate_request_bound_append_rejects_wrong_target_or_foreign_text_before_Delete(self):
  for location in ('target','foreign'):
   with self.subTest(location=location):
    own,_,request=setup(self);h=own.hwp;events=[];old=h.execute;changed=[]
    def execute(name,items):
     ret=old(name,items)
     if name=='InsertText':
      para=h.lists[h.cursor[0]][h.cursor[1]] if location=='target' else h.body[0]
      para['atoms'].append({'cs':para['atoms'][-1]['cs'],'c':'!'});changed.append(True)
     return ret
    h.execute=execute
    with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(events),Control('gso',(0,1,4),EDIT_ID,events)]))):r,error=own.run(request)
    self.assertIsNone(error);self.assertTrue(changed);self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(own.session._native_custody_held)
    self.assertNotIn(('Run','Delete'),h.calls);self.assertEqual(own.cleanup_events(),[])
 def test_added_document_read_fault_has_zero_subsequent_SDK_calls(self):
  own,_,request=setup(self);h=own.hwp;events=[]
  def bad(fmt,option):h.calls.append(('EMPTY_PROOF_FAULT',));return '<broken'
  def head(_):return link([Empty(events,lambda:setattr(h,'GetTextFile',bad)),Control('gso',(0,1,4),EDIT_ID,events)])
  with patch.object(ContainersHwp,'HeadCtrl',property(head)):r,error=own.run(request)
  self.assertIsNone(error);self.assertFalse(r['ok']);self.assertTrue(own.session._native_custody_held)
  self.assertIn(('EMPTY_PROOF_FAULT',),h.calls,'Eligible empty traversal must reach the additional proof read');self.assertEqual(h.calls[h.calls.index(('EMPTY_PROOF_FAULT',))+1:],[]);self.assertEqual(h.executes(),[]);self.assertEqual(own.cleanup_events(),[])
 def test_empty_next_fault_or_nonterminating_chain_never_falls_back(self):
  for fault in ('raise','cycle'):
   with self.subTest(fault=fault):
    m=Lifecycle(self);events=[]
    class BadNext(Empty):
     @property
     def Next(self):
      self.events.append(('Next',''))
      if fault=='raise':raise RuntimeError('next failed')
      return self
    def head(_):return link([Control('gso',(0,0,238),NATIVE,events),BadNext(events)])
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)):r,error=m.own.run(CREATE)
    self.assertIsNone(error);self.assertFalse(r['ok']);self.assertTrue(m.own.session._native_custody_held);self.assertFalse(m.entered)
    self.assertEqual(m.own.cleanup_events(),[]);self.assertEqual([x[1] for x in m.h.executes()],['DrawObjCreatorTextBox'])
