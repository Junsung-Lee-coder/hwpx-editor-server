"""Genuine saved-native HML bytes; SDK/BEFORE snapshots are explicit portable models.
No fixture is a historical immediate GetTextFile or native public feature PASS.
"""
import copy,hashlib,json,os,unittest
from pathlib import Path
from types import MethodType,SimpleNamespace
from xml.etree import ElementTree as E
from app import native_text_containers as nt,text_container_proof as proof
from app.native_numbering_census import CensusError
from local_cli_v1.hwpx_reader import read_package_members
from tests.test_shapeobject_installed_accessor import strict_owned
from tests.native_text_containers_fakes import Ctrl
from tests.native_format_fakes import paragraph

DATA=Path(__file__).parent/'fixtures/native_text_box_constructor'
CREATE={'op':'text_box_create','text':'Synthetic textbox content xxxxx','width_mm':40,'height_mm':20,'expected_pos':[0,0,238],'confirm_layout':True}
def native_root():return E.fromstring((DATA/'native-inline-40x20.hml').read_bytes())
def owner(root):return root.find('BODY/SECTION/P/TEXT/RECTANGLE')
def serial(root):return E.tostring(root,encoding='unicode')
def before_model(*,keep_existing_empty=False):
 root=native_root();run=root.find('BODY/SECTION/P/TEXT');run.remove(owner(root))
 # MODELED before only: untouched seed's last HWPX t is nonempty. No HWPX->HML conversion.
 if not keep_existing_empty:
  assert run[-1].tag=='CHAR' and not (run[-1].text or run[-1].attrib or list(run[-1]))
  run.remove(run[-1])
 return root

def actual_owned(case,kind='healthy',*,session_id='custody-session-0001'):
 owned,shape,trace=strict_owned(case,session_id=session_id);hwp=owned.hwp
 state={'root':before_model(),'created':False,'faulted':False};hwp.cursor=(0,0,238)
 hwp.body=[paragraph('x'*238)];hwp.lists={0:hwp.body,1:[paragraph('')],2:[paragraph('')]};hwp.owners={1:Ctrl('secd','1')}
 ident=owner(native_root()).find('SHAPEOBJECT').get('InstId')
 def get_text(fmt,option):
  hwp.calls.append(('GetTextFile',fmt,option));hwp._identity_xml=serial(state['root']);return hwp._identity_xml
 hwp.GetTextFile=get_text
 class Actions:
  def GetDefault(self,name,hset):hwp.calls.append(('GetDefault',name));return True
  def Execute(self,name,hset):
   hwp.calls.append(('Execute',name))
   if name=='DrawObjCreatorTextBox':
    hwp._created_anchor=(0,0,238);hwp._created_list=2
    state['created']=True;state['root']=native_root();hwp.cursor=(0,0,246);hwp.owners[2]=Ctrl('gso',getattr(hwp,'native_ids',{}).get(ident,ident))
    if kind=='wrong_size':owner(state['root']).find('SHAPEOBJECT/SIZE').set('Width','11338');state['faulted']=True
    if kind=='field_damage':state['root'].find('BODY/SECTION/P/TEXT/FIELDEND').set('Name','UNREQUESTED');state['faulted']=True
   elif name=='InsertText':
    assert hwp.cursor[0]==2
    run=owner(state['root']).find('DRAWINGOBJECT/DRAWTEXT/PARALIST/P/TEXT')
    char=E.SubElement(run,'CHAR') if run.find('CHAR') is None else run.find('CHAR');char.text=hset.Text
    hwp.cursor=(2,0,len(hset.Text))
   else:raise AssertionError('Unexpected modeled action '+name)
   if name=='DrawObjCreatorTextBox':hwp._selected_container=hwp.owners[2]
   hwp.IsModified=True;return True
  def Run(self,name):return hwp._container_run(name)
 hwp.HAction=Actions()
 return owned,trace,state

def retain(case,owned,state,result):
 p=Path(os.environ['TMPDIR']).parent/'native-constructor-evidence';p.mkdir(exist_ok=True)
 (p/(case.id().split('.')[-1]+'.json')).write_text(json.dumps({'case':case.id(),'reply':result,'calls':owned.hwp.calls,
  'created':state['created'],'faulted':state['faulted'],'actual_owning_held':owned.session._native_custody_held,
  'scope':'Real service/owner/HTTP/MCP composition with SDK models; actual fixture native format, BEFORE explicitly modeled; not native acceptance.'},indent=2)+'\n')

class NativeConstructorProof(unittest.TestCase):
 def test_independent_original_data_and_seed_content(self):
  expected={'native-inline-40x20.hml':'54027fba990230dc4e9e533b97d185213f883910bf426a27b164a117aba8c5b4',
   'native-inline-40x20.hwpx':'d3e89abb1e9e9d381c69de9d85c0e4c8ba5973a08ff0653803ea31730a2e2971',
   'native-fields-seed.hwpx':'94713acdd8052a7c886482d005f6b9750356c950e91691b95b3159b0e1d96243'}
  for n,s in expected.items():self.assertEqual(hashlib.sha256((DATA/n).read_bytes()).hexdigest(),s)
  seed=E.fromstring(read_package_members(DATA/'native-fields-seed.hwpx')['Contents/section0.xml'])
  native=before_model();p=native.find('BODY/SECTION/P')
  self.assertEqual(''.join(e.text or '' for e in p.iter('CHAR')),''.join(e.text or '' for e in seed.iter() if e.tag.endswith('}t')))
  self.assertEqual(len(list(p.iter('FIELDBEGIN'))),4);self.assertEqual(len(list(p.iter('FIELDEND'))),4)
 def test_genuine_native_unlinked_zero_pair_has_positive_geometry(self):proof.prove_created_geometry(owner(native_root()),11339,5669)
 def test_genuine_end_constructor_shell_preserves_exact_field_rich_anchor(self):
  before=nt.parse(serial(before_model()));after=nt.parse(serial(native_root()))
  anchor=nt._creation_anchor(before,(0,0,238));nt._prove_created_anchor(after,after['containers'][0]['_owner'],anchor)
 def test_existing_empty_end_marker_is_preserved_not_unconditionally_removed(self):
  before=nt.parse(serial(before_model(keep_existing_empty=True)));after=nt.parse(serial(native_root()))
  nt._prove_created_anchor(after,after['containers'][0]['_owner'],nt._creation_anchor(before,(0,0,238)))
 def test_geometry_wrong_size_inline_degeneracy_transform_and_link_adversaries(self):
  def set_(path,key,value):return lambda r:r.find(path).set(key,value)
  mutations=[set_('SHAPEOBJECT/SIZE','Width','11338'),set_('SHAPEOBJECT/POSITION','TreatAsChar','false'),
   lambda r:[r.set(k,'0') for k in ('X0','X1','X2','X3','Y0','Y1','Y2','Y3')],
   set_('DRAWINGOBJECT/SHAPECOMPONENT','HorzFlip','true'),set_('.//SCAMATRIX','E1','0'),set_('.//ROTMATRIX','E1','-1'),
   set_('.//ROTATIONINFO','Angle','1'),set_('.//PARALIST','LinkListIDNext','1'),set_('.//PARALIST','LinkListID','1'),
   set_('.//PARALIST','LinkListID','00'),set_('.//PARALIST','LinkListID',' 0'),set_('.//PARALIST','LinkListID','-0'),
   lambda r:r.find('.//PARALIST').attrib.pop('LinkListID'),
   set_('DRAWINGOBJECT/SHAPECOMPONENT','CurWidth','0'),set_('DRAWINGOBJECT/SHAPECOMPONENT','GroupLevel','1')]
  for i,mutate in enumerate(mutations):
   with self.subTest(case=i):
    r=copy.deepcopy(owner(native_root()));mutate(r)
    with self.assertRaises(CensusError):proof.prove_created_geometry(r,11339,5669)
 def test_foreign_anchor_empty_shell_and_non_target_adversaries(self):
  before=nt.parse(serial(before_model()));anchor=nt._creation_anchor(before,(0,0,238))
  def extra(root):E.SubElement(root.find('BODY/SECTION/P/TEXT'),'CHAR')
  def prior_empty(root):root.find('BODY/SECTION/P/TEXT').insert(2,E.Element('CHAR'))
  mutations=[lambda r:r.find('BODY/SECTION/P/TEXT')[-1].set('Unexpected','1'),
   lambda r:setattr(r.find('BODY/SECTION/P/TEXT')[-1],'text','unrequested'),extra,prior_empty,
   lambda r:r.find('BODY/SECTION/P/TEXT').set('ChangedFormat','1'),
   lambda r:r.find('BODY/SECTION/P/TEXT/FIELDEND').set('Name','UNREQUESTED')]
  for i,mutate in enumerate(mutations):
   with self.subTest(case=i):
    root=native_root();mutate(root);model=nt.parse(serial(root))
    with self.assertRaises(CensusError):nt._prove_created_anchor(model,model['containers'][0]['_owner'],anchor)
  model=nt.parse(serial(native_root()));wrong=dict(anchor,position=(0,0,237))
  with self.assertRaises(CensusError):nt._prove_created_anchor(model,model['containers'][0]['_owner'],wrong)

class NativeConstructorService(unittest.TestCase):
 def test_real_service_owner_full_create_inserts_requested_text_from_native_format(self):
  owned,trace,state=actual_owned(self);result,error=owned.run(CREATE);retain(self,owned,state,result)
  self.assertIsNone(error);self.assertTrue(result['ok'],result);self.assertFalse(owned.session._native_custody_held)
  self.assertEqual(result['steps'][0]['result']['container']['text'],CREATE['text'])
  self.assertEqual([c[1] for c in owned.hwp.executes()],['DrawObjCreatorTextBox','InsertText'])
 def test_real_owner_first_fault_holds_before_further_sdk_for_geometry_or_fields(self):
  for kind in ('wrong_size','field_damage'):
   with self.subTest(kind=kind):
    owned,trace,state=actual_owned(self,kind);result,error=owned.run(CREATE);retain(self,owned,state,result)
    self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(state['faulted']);self.assertTrue(owned.session._native_custody_held)
    self.assertEqual([c[1] for c in owned.hwp.executes()],['DrawObjCreatorTextBox']);self.assertEqual(owned.hwp.calls[-1][0],'GetTextFile')
    step=result['steps'][0];self.assertTrue(step['dirty']);self.assertTrue(step['mutation_may_have_persisted'])
