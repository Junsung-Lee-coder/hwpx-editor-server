"""Prospective closed-census product proof. No claim about box14 topology/native cause."""
import copy,json,os,unittest
from pathlib import Path
from types import SimpleNamespace
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

def replacement(case,*,after_insert=None,before_delete=None):
 """One real text_container_text_exact through the owning runtime, with an empty-kind census.

 The SDK model only records and injects; it never decides ownership, range or whether Delete may run.
 after_insert: InsertText also writes '!' into the 'target' or a 'foreign' paragraph.
 before_delete: (stage, location) makes the same document change after the Delete callable was
 prepared ('run') or inside the last census before Delete ('census').
 """
 own,trace,request=setup(case);h=own.hwp;raw,action=h.hwp,h.HAction;run,execute,failure=action.Run,h.execute,nt._Native._failure
 lid=next(k for k,c in h.owners.items() if c is not None and c.CtrlID=='gso' and c.GetCtrlInstID()==EDIT_ID)
 text=lambda p:''.join(a['c'] for a in p['atoms'] if 'c' in a)
 document=lambda:(text(h.lists[lid][0]),[text(p) for p in h.body])
 o=SimpleNamespace(own=own,h=h,trace=trace,request=request,lid=lid,initial=document(),start=len(h.calls),events=[],changed=[],faults=[],deletes=[],prepared=[])
 def change(location):
  para=h.lists[lid][0] if location=='target' else h.body[0]
  para['atoms'].append({'cs':para['atoms'][-1]['cs'],'c':'!'});o.changed.append(location);h.calls.append(('SDK_DOCUMENT_CHANGE',location))
 def insert(name,items):
  ret=execute(name,items)
  if name=='InsertText' and after_insert:change(after_insert)
  return ret
 class Prepared:
  def __getattr__(self,name):return getattr(action,name)
  @property
  def Run(self):
   if not o.deletes:
    o.prepared.append(len(h.calls))
    if before_delete and before_delete[0]=='run' and not o.changed:change(before_delete[1])
   def invoke(name):
    if name=='Delete':
     owner=h.owners.get(h.cursor[0])
     o.deletes.append({'selection':h.selection,'owner':(owner.CtrlID,owner.GetCtrlInstID()) if owner is not None else None,'document':document()})
    return run(name)
   return invoke
 class Boundary:
  def __getattr__(self,name):return getattr(raw,name)
  @property
  def HAction(self):return Prepared()
 def census():
  if o.prepared and not o.deletes and before_delete and before_delete[0]=='census' and not o.changed:change(before_delete[1])
 def fault(native,message,site,returned):
  o.faults.append((len(h.calls),len(trace),site,native.issued));return failure(native,message,site,returned)
 h.execute=insert;h.hwp=Boundary()
 with patch.object(ContainersHwp,'HeadCtrl',property(lambda _:link([Empty(o.events,census),Control('gso',(0,1,4),EDIT_ID,o.events)]))),patch.object(nt._Native,'_failure',fault):o.reply,o.error=own.run(request)
 o.calls=h.calls[o.start:];o.mutators=[c[:2] for c in o.calls if c[0] in ('Run','Execute')];o.selects=[c for c in o.calls if c[0]=='select_text']
 return o

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
 def test_post_insert_wrong_target_or_foreign_text_holds_after_the_one_legal_Delete_without_follow_on(self):
  # The replacement deletes the proven old range first and inserts afterwards, so damage done by
  # InsertText can only be seen after that one Delete. It must then hold with no further SDK call.
  for location in ('target','foreign'):
   with self.subTest(location=location):
    o=replacement(self,after_insert=location);h,r,size=o.h,o.reply,len(o.request['expected_text'])
    self.assertIsNone(o.error);self.assertEqual(o.mutators,[('Run','Delete'),('Execute','InsertText')],'one Delete, before the one InsertText, and no second destructive call')
    self.assertEqual(o.selects,[('select_text',0,0,0,size,o.lid)]);self.assertEqual(o.deletes,[{'selection':(o.lid,0,0,o.lid,0,size),'owner':('gso',EDIT_ID),'document':o.initial}],'Delete ran on the exact old range of the bound native owner while the document was unchanged')
    self.assertEqual(o.changed,[location]);self.assertFalse(r['ok']);self.assertTrue(r['dirty']);self.assertTrue(o.own.session._native_custody_held)
    self.assertEqual([f[2:] for f in o.faults],[('complete_document_preservation',True)]);self.assertEqual((len(h.calls),len(o.trace)),o.faults[0][:2],'no SDK call of any kind after the detected damage')
    after=o.calls[o.calls.index(('SDK_DOCUMENT_CHANGE',location))+1:];self.assertEqual([c for c in after if c[0] not in ('GetPos','GetSelectedPos','get_text_file')],[],'only the proof reads follow the damaging InsertText')
    d=r['steps'][0].get('failure_diagnostic') or {};self.assertEqual((d.get('causal_class'),d.get('native_site')),('NativeNumberingMutationError','container.complete_document_preservation'));self.assertEqual(o.own.cleanup_events(),[])
 def test_target_or_foreign_text_drift_up_to_the_final_census_proof_issues_no_Delete(self):
  # Counterpart before the first mutator: the same target/foreign change, made by the SDK after the
  # Delete callable is prepared or inside the last census before Delete, is caught by the proof read
  # of the empty-kind census. Neither Delete nor InsertText may then be issued. The first run without
  # any change is the control: the same instrumented scenario succeeds with one exact Delete.
  o=replacement(self);size=len(o.request['expected_text'])
  self.assertIsNone(o.error);self.assertTrue(o.reply['ok'],o.reply);self.assertFalse(o.own.session._native_custody_held);self.assertEqual((o.faults,o.changed,len(o.prepared)),([],[],1))
  self.assertEqual(o.mutators,[('Run','Delete'),('Execute','InsertText')]);self.assertEqual(o.deletes,[{'selection':(o.lid,0,0,o.lid,0,size),'owner':('gso',EDIT_ID),'document':o.initial}])
  for stage in ('run','census'):
   for location in ('target','foreign'):
    with self.subTest(stage=stage,location=location):
     o=replacement(self,before_delete=(stage,location));h,r=o.h,o.reply
     self.assertIsNone(o.error);self.assertEqual(o.changed,[location],'the SDK change must actually fire before Delete');self.assertFalse(r['ok']);self.assertTrue(o.own.session._native_custody_held)
     self.assertEqual(o.mutators,[],'no Delete and no InsertText after document drift before Delete');self.assertEqual(o.deletes,[])
     self.assertEqual([f[2:] for f in o.faults],[('binding.document',False)]);self.assertEqual((len(h.calls),len(o.trace)),o.faults[0][:2],'no SDK call after the first fault')
     d=r['steps'][0].get('failure_diagnostic') or {};self.assertEqual((d.get('native_site'),d.get('native_binding_branch'),d.get('native_binding_phase')),('container.binding.document','full_signature','owner.selection_identity'))
     # Request intent was announced before this final proof, so the refusal keeps the conservative dirty debt.
     self.assertTrue(r['dirty']);self.assertEqual(o.own.cleanup_events(),[])
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
