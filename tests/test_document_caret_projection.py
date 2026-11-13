"""Serialized caret is view state; logical document and live witness stay separate."""
import copy,unittest,xml.etree.ElementTree as E
from unittest.mock import patch
from app import native_text_containers as nt
from tests.test_constructor_state_contract import Lifecycle,CREATE
from tests.test_native_constructor_postcreate import native_root
from tests.test_empty_ctrlid_product import create_head,ContainersHwp

def caret(root):return root.find('HEAD/DOCSETTING/CARETPOS')
def parse(root):return nt.parse(E.tostring(root,encoding='unicode'))
def change_title(root):
 head=root.find('HEAD');summary=head.find('DOCSUMMARY')
 if summary is None:summary=E.SubElement(head,'DOCSUMMARY')
 title=summary.find('TITLE')
 if title is None:title=E.SubElement(summary,'TITLE')
 title.text=(title.text or '')+' Authored change'

class CaretSerialization(Lifecycle):
 """Existing native-authored format/current SDK model; serializer-only overlay.
 No native observations are invented: h.cursor is this DECLARED SDK's position.
 """
 def __init__(self,case,*,sid='custody-session-0001',mode=4,damage=None,move=None):
  super().__init__(case,mode,sid=sid);original=self.h.GetTextFile;self.applied=[];self.read_states=[]
  def text(fmt,option):
   value=original(fmt,option)
   if option:return value
   root=E.fromstring(value);node=caret(root);assert node is not None
   for k,v in zip(('List','Para','Pos'),self.h.cursor):node.set(k,str(v))
   self.read_states.append(self.h.cursor)
   if self.entered and damage is not None:damage(root);self.applied.append(True)
   result=E.tostring(root,encoding='unicode')
   if self.entered and move is not None and not self.applied:
    move(self);self.applied.append(True)
   return result
  self.h.GetTextFile=text

class DocumentCaretProjection(unittest.TestCase):
 def test_only_unique_documented_view_coordinates_are_logically_invariant(self):
  root=native_root();base=nt._signature(parse(root));self.assertIsNotNone(caret(root))
  for attr,value in (('List','0'),('List','42'),('Para','7'),('Pos','2147483647')):
   with self.subTest(attr=attr,value=value):
    other=copy.deepcopy(root);caret(other).set(attr,value);self.assertEqual(base,nt._signature(parse(other)))
 def test_summary_begin_number_and_other_attributes_remain_meaningful(self):
  root=native_root();base=nt._signature(parse(root))
  for field in ('title','date','begin','other_caret_attr','head','foreign_pos'):
   with self.subTest(field=field):
    other=copy.deepcopy(root)
    if field=='title':change_title(other)
    elif field=='date':
     summary=other.find('HEAD/DOCSUMMARY');date=summary.find('DATE');date.text=(date.text or '')+' changed'
    elif field=='begin':other.find('HEAD/DOCSETTING/BEGINNUMBER').set('Page','8')
    elif field=='other_caret_attr':caret(other).set('AuthoredExtension','changed')
    elif field=='head':other.find('HEAD').set('SecCnt','2')
    else:other.find('BODY/SECTION/P').set('Pos','changed')
    self.assertNotEqual(base,nt._signature(parse(other)))
 def test_malformed_coordinate_missing_field_and_node_presence_are_not_masked(self):
  root=native_root();base=nt._signature(parse(root))
  for value in ('','-1','01','true','2147483648','not-a-position'):
   with self.subTest(value=value):
    other=copy.deepcopy(root);caret(other).set('List',value);self.assertNotEqual(base,nt._signature(parse(other)))
  for form in ('missing_attr','missing_node','text','child'):
   other=copy.deepcopy(root);node=caret(other)
   if form=='missing_attr':node.attrib.pop('Pos')
   elif form=='missing_node':other.find('HEAD/DOCSETTING').remove(node)
   elif form=='text':node.text='Authored content'
   else:E.SubElement(node,'EXTENSION').text='Authored content'
   self.assertNotEqual(base,nt._signature(parse(other)),form)
 def test_extended_caret_shape_cannot_gain_runtime_projection_by_known_field_names(self):
  root=native_root();caret(root).set('AuthoredExtension','preserve');base=nt._signature(parse(root))
  caret(root).set('Pos','381');self.assertNotEqual(base,nt._signature(parse(root)))
 def test_wrong_path_namespace_and_duplicate_nodes_do_not_receive_projection(self):
  for form in ('namespace','wrong_parent','duplicate'):
   with self.subTest(form=form):
    root=native_root();node=caret(root)
    if form=='namespace':node.tag='{urn:foreign}CARETPOS'
    elif form=='wrong_parent':root.find('HEAD/DOCSETTING').remove(node);root.find('HEAD/DOCSUMMARY').append(node)
    else:root.find('HEAD/DOCSETTING').append(copy.deepcopy(node))
    base=nt._signature(parse(root));node.set('Pos','314');self.assertNotEqual(base,nt._signature(parse(root)))
 def test_native_authored_constructor_with_live_serializer_coordinates_completes_exact_text(self):
  for mode in (0,4,20):
   with self.subTest(mode=mode):
    m=CaretSerialization(self,mode=mode);events=[]
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events))):r,e=m.own.run(CREATE)
    self.assertIsNone(e);self.assertTrue(r['ok'],r);self.assertTrue(m.entered);self.assertFalse(m.own.session._native_custody_held)
    self.assertIn(CREATE['text'],''.join(m.data['root'].itertext()));self.assertGreater(len(set(m.read_states)),1);self.assertEqual(m.own.cleanup_events(),[])
 def test_exact_edits_for_all_families_allow_planned_view_transitions_not_content_damage(self):
  from tests.test_native_container_identity import setup
  for family in ('text_box','caption','footnote'):
   with self.subTest(family=family):
    own,_,request=setup(self,family);h=own.hwp;original=h.GetTextFile
    def text(fmt,option):
     value=original(fmt,option)
     if option:return value
     root=E.fromstring(value);head=root.find('HEAD');setting=head.find('DOCSETTING')
     if setting is None:setting=E.SubElement(head,'DOCSETTING')
     node=setting.find('CARETPOS')
     if node is None:node=E.SubElement(setting,'CARETPOS')
     for key,value in zip(('List','Para','Pos'),h.cursor):node.set(key,str(value))
     return E.tostring(root,encoding='unicode')
    h.GetTextFile=text;r,e=own.run(request)
    self.assertIsNone(e);self.assertTrue(r['ok'],r);self.assertFalse(own.session._native_custody_held);self.assertEqual(own.cleanup_events(),[])
 def test_read_mutating_live_caret_selection_or_mode_still_HOLDs(self):
  for what in ('caret','selection','mode'):
   with self.subTest(what=what):
    def move(m):
     if what=='caret':m.h.cursor=(m.h.cursor[0],0,1)
     elif what=='selection':m.h.selection=(m.h.cursor[0],0,0,m.h.cursor[0],0,1);m.mode=1
     else:m.mode=2
    m=CaretSerialization(self,move=move);events=[]
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events))):r,e=m.own.run(CREATE)
    self.assertIsNone(e);self.assertTrue(m.applied);self.assertFalse(r['ok']);self.assertTrue(m.own.session._native_custody_held);self.assertEqual(m.own.cleanup_events(),[])
    self.assertEqual([c[1] for c in m.h.executes()],['DrawObjCreatorTextBox'])
 def test_meaningful_document_damage_during_fresh_read_is_not_view_state(self):
  def width(root):root.find('BODY/SECTION/P/TEXT/RECTANGLE/SHAPEOBJECT/SIZE').set('Width','1')
  def identity(root):root.find('BODY/SECTION/P/TEXT/RECTANGLE/SHAPEOBJECT').set('InstId','7')
  for damage in (change_title,width,identity):
   with self.subTest(damage=damage.__name__):
    m=CaretSerialization(self,damage=damage);events=[]
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events))):r,e=m.own.run(CREATE)
    self.assertIsNone(e);self.assertTrue(m.applied);self.assertFalse(r['ok']);self.assertTrue(m.own.session._native_custody_held)
    self.assertEqual([c[1] for c in m.h.executes()],['DrawObjCreatorTextBox']);self.assertEqual(m.own.cleanup_events(),[])
