"""Independent inert desktop IDHwpCtrlCode/ParameterSet model for identity tests.
Coordinates follow documented character/inline/extended widths; native instance
IDs are configurable independently of all HWPML identifiers. No SDK or native claim.
"""
from xml.etree import ElementTree as E
GSO={'RECTANGLE','ELLIPSE','ARC','POLYGON','CURVE','LINE','CONNECTLINE','PICTURE','OLE','CONTAINER'}
OTHER={'SECDEF':'secd','COLDEF':'cold','TABLE':'tbl','FOOTNOTE':'fn','ENDNOTE':'en','HEADER':'head','FOOTER':'foot'}
class Anchor:
 def __init__(self,point):self.point=point
 def Item(self,key):return self.point[('List','Para','Pos').index(key)]
class Control:
 def __init__(self,hwp,kind,point,ident):self.hwp,self.kind,self.point,self.ident=hwp,kind,point,ident;self.next=None
 @property
 def CtrlID(self):self.hwp.calls.append(('control.CtrlID',self.kind));return self.kind
 @property
 def Next(self):self.hwp.calls.append(('control.Next',));return self.next
 def GetAnchorPos(self,which):
  assert which==0;self.hwp.calls.append(('control.GetAnchorPos',self.kind,self.point));return Anchor(self.point)
 def GetCtrlInstID(self):
  self.hwp.calls.append(('control.GetCtrlInstID',self.kind,self.ident));return self.ident

def chain(hwp):
 hwp.calls.append(('HeadCtrl',));source=getattr(hwp,'_identity_xml',None)
 if not source:raise AssertionError('Identity model must consume a complete emitted HWPML before HeadCtrl')
 root=E.fromstring(source);nodes=[]
 def text_width(e):
  n=len(e.text or '')
  for c in e:
   n+=8 if c.tag=='TAB' else 1 if c.tag in ('LINEBREAK','HYPHEN','NBSPACE','FWSPACE') else 8
   n+=len(c.tail or '')
  return n
 for pi,p in enumerate(root.findall('BODY/SECTION/P')):
  off=0
  for run in p:
   if run.tag!='TEXT':continue
   for child in run:
    tag=child.tag
    if tag=='CHAR':off+=text_width(child)
    else:
     kind='gso' if tag in GSO else OTHER.get(tag)
     if kind:
      ident=child.get('InstId') or child.get('InstID')
      shape=child.find('SHAPEOBJECT')
      if shape is not None:ident=shape.get('InstId')
      ident=getattr(hwp,'native_ids',{}).get(ident,ident) if kind in ('gso','fn') else ''
      nodes.append(Control(hwp,kind,(0,pi,off),ident))
     off+=8
    off+=len(child.tail or '')
 for a,b in zip(nodes,nodes[1:]):a.next=b
 alter=getattr(hwp,'identity_controls',None)
 if alter is not None:nodes=alter(nodes)
 for i,c in enumerate(nodes):c.next=nodes[i+1] if i+1<len(nodes) else None
 return nodes[0] if nodes else None
