"""Independent strict desktop-control models. No XML/native ID equivalence oracle.
Coordinates/IDs supplied explicitly by these cases; no production atom algorithm
or candidate chain() used to generate the SDK answers. Not real native/STA proof.
"""
import copy,hashlib,json,os,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as E
from app import native_text_containers as nt,native_format as nf
from tests.test_native_container_identity import setup
from tests.native_text_containers_fakes import ContainersHwp
from tests.test_native_constructor_postcreate import native_root,owner,serial
IMPORTED={'path':__file__,'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
def save(name,data):
 p=Path(os.environ['TMPDIR']).parent/(name+'.json');p.write_text(json.dumps({'independent_test':IMPORTED,**data},indent=1)+'\n')
class Anchor:
 __slots__=('point','events')
 def __init__(self,point,events):self.point,self.events=point,events
 def Item(self,key):
  if type(key) is not str or key not in ('List','Para','Pos'):raise AssertionError('Only exact BSTR ListParaPos Item fields')
  self.events.append(('anchor.Item',key));return self.point[('List','Para','Pos').index(key)]
class Control:
 __slots__=('kind','point','ident','next','events','effect','fired')
 def __init__(self,kind,point,ident,events,effect=None):self.kind,self.point,self.ident,self.next,self.events,self.effect,self.fired=kind,point,ident,None,events,effect,False
 @property
 def CtrlID(self):self.events.append(('CtrlID',self.kind));return self.kind
 @property
 def Next(self):self.events.append(('Next',self.kind));return self.next
 def GetAnchorPos(self,which):
  if type(which) is not int or which!=0:raise AssertionError('Exact LONG0 immediate-parent route only')
  if self.kind not in ('gso','fn'):raise AssertionError('Forbidden foreign target-only anchor getter')
  self.events.append(('GetAnchorPos',self.kind))
  if self.effect is not None and not self.fired:self.fired=True;self.effect()
  return Anchor(self.point,self.events)
 def GetCtrlInstID(self):
  if self.kind not in ('gso','fn'):raise AssertionError('Forbidden foreign target-only native ID getter')
  self.events.append(('GetCtrlInstID',self.kind,self.ident));return self.ident

def link(nodes):
 for i,c in enumerate(nodes):c.next=nodes[i+1] if i+1<len(nodes) else None
 return nodes[0] if nodes else None
class IndependentIdentity(unittest.TestCase):
 def test_independent_manual_census_all_target_pairs_disjoint_and_nested_foreign(self):
  rows=[]
  for kind in ('text_box','caption','footnote'):
   with self.subTest(kind=kind):
    own,trace,request=setup(self,kind);h=own.hwp;nativeid='900000031';h.native_ids={'101':nativeid};h._number_cells(h.body);events=[];targetkind='fn' if kind=='footnote' else 'gso'
    # Known fixture: paragraph1 text "lead" then one 8-unit owner, native List0.
    nodes=[Control('secd',(0,0,0),'',events),Control(targetkind,(0,1,4),nativeid,events),Control(targetkind,(7,0,2),'900000032',events)]
    head=link(nodes)
    with patch.object(ContainersHwp,'HeadCtrl',property(lambda _h:head)):result,error=own.run(request)
    save('manual-'+kind,{'result':result,'events':events,'calls':h.calls,'held':own.session._native_custody_held})
    self.assertIsNone(error);self.assertTrue(result['ok'],result);self.assertFalse(own.session._native_custody_held);self.assertEqual(h.cursor,(0,0,0))
    self.assertFalse(any(x[0] in ('GetAnchorPos','GetCtrlInstID') and x[1]=='secd' for x in events));rows.append(kind)
  save('manual-pairs-summary',{'passed_kinds':rows,'native_ids_independent_of_XML':True,'nested_nonbody_samekind_has_unique_id_anchor':True})
 def test_identical_drawings_bind_by_element_and_shared_box_caption_remains_ineligible(self):
  root=native_root();run=root.find('BODY/SECTION/P/TEXT');first=owner(root);second=copy.deepcopy(first);second.find('SHAPEOBJECT').set('InstId','2000000001');run.insert(len(run)-1,second)
  m=nt.parse(serial(root));one,two=m['containers'];a=nt._body_control_anchors(m)
  self.assertIs(a[('gso',(0,0,238))],one['_owner']);self.assertIs(a[('gso',(0,0,246))],two['_owner'])
  # Otherwise byte-identical drawings may still have distinct serialized InstId;
  # do not invent an existing digest normalizer that strips this identifier.
  c1,c2=copy.deepcopy(one['_owner']),copy.deepcopy(two['_owner']);c1.find('SHAPEOBJECT').attrib.pop('InstId');c2.find('SHAPEOBJECT').attrib.pop('InstId');self.assertEqual(E.tostring(c1),E.tostring(c2))
  events=[];head=link([Control('secd',(0,0,0),'',events),Control('gso',(0,0,238),'800000001',events),Control('gso',(0,0,246),'800000002',events)])
  native=nt._Native(SimpleNamespace(hwp=SimpleNamespace(HeadCtrl=head)))
  self.assertEqual(nt._binding(native,m,one)[2],'800000001');self.assertEqual(nt._binding(native,m,two)[2],'800000002')
  # Same rectangle has box + caption lists: inventory/read may expose both,
  # but neither becomes editable by borrowing the shared control identity.
  root=native_root();o=owner(root);caption=E.SubElement(o,'CAPTION');pl=E.SubElement(caption,'PARALIST');p=E.SubElement(pl,'P',{'ParaShape':'0','Style':'0'});text=E.SubElement(p,'TEXT',{'CharShape':'0'});E.SubElement(text,'CHAR').text='Caption'
  m=nt.parse(serial(root));self.assertEqual(len(m['containers']),2);self.assertTrue(all(not i['editable'] for i in m['containers']));self.assertEqual(len(nt._body_control_anchors(m)),1)
  save('manual-identical-and-shared',{'otherwise_identical_drawing_contents_distinct_XML_IDs':True,'two_disjoint_native_ids_observed':True,'shared_two_lists_not_editable':True})
 def test_manual_required_invalid_target_never_becomes_foreign_skip_or_followon(self):
  own,trace,request=setup(self);events=[];head=link([Control('secd',(0,0,0),'',events),Control('gso',(0,1,4),'',events)])
  with patch.object(ContainersHwp,'HeadCtrl',property(lambda _h:head)):result,error=own.run(request)
  save('manual-required-fault',{'result':result,'events':events,'calls':own.hwp.calls,'held':own.session._native_custody_held})
  self.assertIsNone(error);self.assertFalse(result['ok']);self.assertTrue(own.session._native_custody_held);self.assertEqual(own.hwp.executes(),[])
  self.assertEqual(events[-1],('GetCtrlInstID','gso',''));self.assertEqual(result['steps'][0]['failure_diagnostic']['native_site'],'container.control.GetCtrlInstID')
  self.assertFalse(result['steps'][0]['dirty']);self.assertEqual(own.cleanup_events(),[])
 def test_upfront_anchor_getter_caret_drift_must_hold_before_navigation_and_edit(self):
  own,trace,request=setup(self);h=own.hwp;events=[]
  def drift():h.cursor=(0,2,0);h.calls.append(('UNREQUESTED_GETTER_CARET_DRIFT',(0,2,0)))
  head=link([Control('secd',(0,0,0),'',events),Control('gso',(0,1,4),'900000001',events,effect=drift)])
  with patch.object(ContainersHwp,'HeadCtrl',property(lambda _h:head)):result,error=own.run(request)
  marker=next(i for i,x in enumerate(h.calls) if x[0]=='UNREQUESTED_GETTER_CARET_DRIFT');following=h.calls[marker+1:]
  save('upfront-caret-drift',{'actual_result':result,'error_is_none':error is None,'owning_held':own.session._native_custody_held,'events':events,'calls':h.calls,'after_drift_calls':following,'executed_actions':h.executes(),'final_cursor':h.cursor,'expected':'successful identity read must be state-preserving; unexpected caret change must latch before SetPos/select/edit, not be erased by scan'})
  self.assertIsNone(error);self.assertFalse(result['ok'],'Upfront GetAnchorPos moved caret but list scan silently erased it and continued')
  self.assertTrue(own.session._native_custody_held);self.assertEqual(h.executes(),[])
  self.assertFalse(any(x[0] in ('SetPos','set_pos','SelectText','select_text','Execute') for x in following))
