"""Synthetic external COM, bound to installed TLB data; never native evidence.
Typed HSet holders and legacy IDHwpParameterSet objects are not interchangeable.
Defaults represent the selected picture; only actual property puts cause edits.
"""
import contextlib,sys,types
from typing import Any
from unittest.mock import patch
import xml.etree.ElementTree as ET
IIDS={
 'HAction':'{1A10DC8D-1155-4EE3-9EA2-1A160CDE2735}',
 'HParameterSet':'{F4D24DDE-F9FA-423C-B4FE-4BE0CF135EC5}',
 'HShapeObject':'{44D40483-8167-452B-B33A-EE0CB6993F61}',
 'HDrawImageAttr':'{DB3A0E9F-46DA-4F29-B060-42EA8959BFB7}',
 'HPictureChange':'{524AD7AC-5F82-4D44-B8A0-3326F8E49E0C}',
 'HSet':'{C64C95D7-8A60-420E-BE79-5D0D23C63316}'}
def local(e):return e.tag.rsplit('}',1)[-1].lower()
def first(e,n):return next(x for x in e.iter() if local(x)==n)
class IID:
 def __init__(self,value):
  if type(value) is not str:raise TypeError('IID string')
  self.value=value.upper()
 def __eq__(self,other):return type(other) is IID and self.value==other.value
 def __str__(self):return self.value
class TypeAttr:
 def __init__(self,iid=None):self.iid=IID(iid or IIDS['HSet'])
class TypeInfo:
 def __init__(self,name):self.name=name
 def GetTypeAttr(self):return TypeAttr(IIDS[self.name])
class Ole:
 def __init__(self,name):self.name=name
 def GetTypeInfo(self):return TypeInfo(self.name)
@contextlib.contextmanager
def com_modules():
 # External pywin32 only; no product guard, snapshot, custody or projection stub.
 p=types.ModuleType('pythoncom');setattr(p,'TYPEATTR',TypeAttr)
 q=types.ModuleType('pywintypes');setattr(q,'IID',IID)
 with patch.dict(sys.modules,{'pythoncom':p,'pywintypes':q}):yield
class HSet:
 def __init__(self,root):self.root=root;self._oleobj_=Ole('HSet')
class Typed:
 TYPES={
  'HShapeObject':{'TextFlow':18,'TreatAsChar':18,'TextWrap':18,'HorzOffset':3,'VertOffset':3,'Width':19,'Height':19},
  'HDrawImageAttr':{'OriginalSizeX':19,'OriginalSizeY':19,'SkipLeft':19,'SkipRight':19,'SkipTop':19,'SkipBottom':19},
  'HPictureChange':{'PicturePath':8,'PictureEmbed':18}}
 def __init__(self,owner,name):
  object.__setattr__(self,'owner',owner);object.__setattr__(self,'name',name)
  object.__setattr__(self,'_oleobj_',Ole(name));object.__setattr__(self,'values',{})
  object.__setattr__(self,'written',set());object.__setattr__(self,'HSet',HSet(self))
  if name=='HShapeObject':object.__setattr__(self,'ShapeDrawImageAttr',Typed(owner,'HDrawImageAttr'))
 def __getattr__(self,key):
  if key not in self.TYPES[self.name]:raise AttributeError(key)
  self.owner.events.append('get:'+self.name+'.'+key)
  if key not in self.values:raise RuntimeError('GetDefault not completed for '+key)
  faults=getattr(self.owner,'typed_read_faults',{})
  return faults.get((self.name,key),self.values[key])
 def __setattr__(self,key,value: Any):
  if key not in self.TYPES[self.name]:raise AttributeError('Undeclared typed property '+key)
  vt=self.TYPES[self.name][key]
  if (vt==8 and type(value) is not str) or (vt!=8 and type(value) is not int):raise TypeError(key)
  if vt==18 and not 0<=value<2**16:raise OverflowError(key)
  if vt==19 and not 0<=value<2**32:raise OverflowError(key)
  if vt==3 and not -2**31<=value<2**31:raise OverflowError(key)
  self.owner.events.append('put:'+self.name+'.'+key)
  if self.owner.setter_result is not None:raise RuntimeError('injected typed property put failure')
  self.values[key]=value;self.written.add(key)
 def load(self,values):self.values.clear();self.values.update(values);self.written.clear()
class Parameters:
 def __init__(self,owner):
  self._oleobj_=Ole('HParameterSet')
  self.HShapeObject=Typed(owner,'HShapeObject')
  self.HPictureChange=Typed(owner,'HPictureChange')
def default(owner,name,pset):
 if type(pset) is not HSet or pset.root.owner is not owner:raise TypeError('HAction requires typed-owner HSet, not legacy CreateSet')
 root=pset.root
 if root.name!=('HPictureChange' if name=='PictureChange' else 'HShapeObject'):raise TypeError('Action/family mismatch')
 owner.events.append('GetDefault:'+name)
 p=next(p for p in owner.root.iter() if local(p)=='picture' and first(p,'shapeobject').get('InstId')==owner.selected.ident)
 shape=first(p,'shapeobject');pos=first(shape,'position');size=first(shape,'size')
 if name=='PictureChange':root.load({'PicturePath':'','PictureEmbed':1})
 else:
  # Independent synthetic effective state, also when native serialization omits it.
  flow_names=('BothSides','LeftOnly','RightOnly','LargestOnly')
  observed_flow=flow_names.index(shape.get('TextFlow')) if 'TextFlow' in shape.attrib else owner.effective_text_flow
  root.load({'TreatAsChar':1 if pos.get('TreatAsChar')=='true' else 0,'TextWrap':0 if shape.get('TextWrap')=='Square' else 1,
   'TextFlow':observed_flow,'HorzOffset':int(pos.get('HorzOffset','0')),'VertOffset':int(pos.get('VertOffset','0')),'Width':int(size.get('Width')),'Height':int(size.get('Height'))})
  dims=first(p,'imagedim');clip=first(p,'imageclip');sx=int(dims.get('Width'));sy=int(dims.get('Height'))
  root.ShapeDrawImageAttr.load({'OriginalSizeX':sx,'OriginalSizeY':sy,'SkipLeft':int(clip.get('Left')),'SkipRight':sx-int(clip.get('Right')),'SkipTop':int(clip.get('Top')),'SkipBottom':sy-int(clip.get('Bottom'))})
 return getattr(owner,'default_result',True)
def execution_values(owner,name,pset):
 if type(pset) is not HSet or pset.root.owner is not owner:raise TypeError('Legacy parameter at typed Execute')
 root=pset.root
 if root.name!=('HPictureChange' if name=='PictureChange' else 'HShapeObject'):raise TypeError('Wrong Execute family')
 d=dict(root.values)
 if root.name=='HShapeObject' and root.ShapeDrawImageAttr.written:
  d={'ShapeDrawImageAttr':types.SimpleNamespace(data=dict(root.ShapeDrawImageAttr.values))}
 elif root.name=='HShapeObject':d={k:v for k,v in d.items() if k in root.written}
 return types.SimpleNamespace(data=d)
