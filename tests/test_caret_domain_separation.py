"""Qualified logical view domain must be impossible to forge with raw XML data."""
import copy,unittest,xml.etree.ElementTree as E
from unittest.mock import patch
from app import native_text_containers as nt
from tests.test_native_constructor_postcreate import native_root
from tests.test_document_caret_projection import CaretSerialization,caret
from tests.test_empty_ctrlid_product import ContainersHwp,create_head
from tests.test_constructor_state_contract import CREATE

def parse(root):return nt.parse(E.tostring(root,encoding='unicode'))
def variants():
 def marker(r):
  for k in ('List','Para','Pos'):caret(r).set(k,'<runtime-caret-coordinate>')
 def literal(r):
  for k in ('List','Para','Pos'):caret(r).set(k,'[true,null,"runtime-view"]')
 def missing(r):caret(r).attrib.pop('List')
 def empty(r):caret(r).attrib.clear()
 def tag(r):caret(r).tag='CaretPos'
 def namespace(r):caret(r).tag='{urn:foreign}CARETPOS'
 def wrongpath(r):
  h=r.find('HEAD');s=h.find('DOCSETTING');n=s.find('CARETPOS');s.remove(n);h.append(n)
 def duplicate(r):r.find('HEAD/DOCSETTING').append(copy.deepcopy(caret(r)))
 def extension(r):caret(r).set('AuthoredExtension','preserve')
 def text(r):caret(r).text='authored'
 def child(r):E.SubElement(caret(r),'Foreign').set('Value','preserve')
 return [marker,literal,missing,empty,tag,namespace,wrongpath,duplicate,extension,text,child]

class CaretDomainSeparation(unittest.TestCase):
 def test_unqualified_XML_cannot_forge_projected_structural_prehash_domain(self):
  good=parse(native_root())
  for damage in variants():
   with self.subTest(shape=damage.__name__):
    root=native_root();damage(root);raw=parse(root);self.assertIsNone(nt._document_view_caret(raw))
    # Inspect actual typed prehash trees, NOT only96bit inequality. XML values
    # cannot add a Python shell-vector slot computed from eligibility identity.
    with patch.object(nt,'_hash',side_effect=lambda tree:tree):
     self.assertNotEqual(nt._signature(good),nt._signature(raw))
    self.assertNotEqual(nt._signature(good),nt._signature(raw))
 def test_unsupported_serialization_HOLDs_before_dependent_text_insertion(self):
  for damage in variants():
   with self.subTest(shape=damage.__name__):
    m=CaretSerialization(self,damage=damage);events=[]
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events))):r,e=m.own.run(CREATE)
    self.assertIsNone(e);self.assertTrue(m.applied);self.assertFalse(r['ok']);self.assertTrue(m.own.session._native_custody_held);self.assertEqual(m.own.cleanup_events(),[])
    self.assertNotIn('InsertText',[x[1] for x in m.h.executes()])
 def test_raw_content_significance_and_original_XML_model_immutability(self):
  for damage in [None,*variants()]:
   with self.subTest(shape=damage.__name__ if damage else 'qualified'):
    root=native_root()
    if damage:damage(root)
    model=parse(root);before=E.tostring(model['root']);source=model['_source_hml'];sig=nt._signature(model)
    self.assertEqual(E.tostring(model['root']),before);self.assertEqual(model['_source_hml'],source)
    # A meaningful authored header attribute remains distinct in BOTH domains.
    model['root'].find('HEAD').set('AuthoredHeader','changed')
    self.assertNotEqual(sig,nt._signature(model))
