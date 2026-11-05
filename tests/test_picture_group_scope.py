"""C581-SCOPE-1: real public service/STA with external SDK data, not native proof."""
import unittest
from app import native_picture as pic
from tests.test_picture_anchored_selection import AnchoredPicture
from tests.test_picture_n2_contract import owned,req,command


def field(h,name):
 return next(e for e in h.root.iter() if e.tag.rsplit('}',1)[-1].lower()==name)


class GroupScope(unittest.TestCase):
 def rejected(self,h):
  with owned(h) as o:
   result=command(o,req(h,'picture_layout_exact',mode='square'))
   self.assertFalse(result['ok'],result)
   self.assertEqual(o.cleanup_events(),[])
  self.assertEqual(h.counts['SetPosBySet'],0)
  self.assertEqual(h.counts['HAction.Run.SelectCtrlFront'],0)
  self.assertEqual(h.counts['Execute'],0)
  self.assertFalse(any('put' in x for x in h.trace),h.trace)
  self.assertFalse(pic.parse_pictures(h.get_text_file('HWPML2X',''))['pictures'][0]['supported'])

 def test_authoritative_component_nonzero_cannot_hide_behind_shape_zero(self):
  h=AnchoredPicture();field(h,'shapecomponent').set('GroupLevel','1');field(h,'shapeobject').set('GroupLevel','0')
  self.rejected(h)

 def test_missing_malformed_and_noncanonical_component_values_refuse_before_selection(self):
  for value in (None,'','1','-1','00','01','0.0',' 0','false','unknown'):
   with self.subTest(value=value):
    h=AnchoredPicture();c=field(h,'shapecomponent')
    if value is None:c.attrib.pop('GroupLevel',None)
    else:c.set('GroupLevel',value)
    self.rejected(h)

 def test_wrong_case_or_namespace_and_conflicting_shape_grouping_refuse(self):
  for part,key,value in (('shapecomponent','grouplevel','1'),('shapecomponent','GROUPLEVEL','0'),('shapecomponent','{urn:unqualified}GroupLevel','0'),('shapeobject','GroupLevel','1'),('shapeobject','grouplevel','0')):
   with self.subTest(part=part,key=key,value=value):
    h=AnchoredPicture();field(h,part).set(key,value);self.rejected(h)

 def test_real_component_zero_with_absent_or_consistent_shape_stays_positive(self):
  for shadow in (None,'0'):
   with self.subTest(shadow=shadow):
    h=AnchoredPicture();c=field(h,'shapecomponent');self.assertEqual(c.get('GroupLevel'),'0')
    s=field(h,'shapeobject');s.attrib.pop('GroupLevel',None)
    if shadow is not None:s.set('GroupLevel',shadow)
    with owned(h) as o:
     result=command(o,req(h,'picture_layout_exact',mode='square'));self.assertTrue(result['ok'],result)
    self.assertEqual(h.counts['SetPosBySet'],1);self.assertEqual(h.counts['HAction.Run.SelectCtrlFront'],1);self.assertEqual(h.counts['Execute'],1)
