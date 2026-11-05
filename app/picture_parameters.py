"""One typed Automation family, bound to the installed HwpObject.tlb.

Metadata: typelib 7D2B6F3C-1D95-4E0C-BF5A-5EE564186FBC v1.0,
SHA256 047f64bd2714ea2eabb63d5771fea83a1f239a73393548f55d1771df116d9bf1.
Current data projection 7bf4e208ec87f7a0bb6e5cd9cc67878d25283b4338e8ba13fc7352a5f83bf339.
IDispatch signature, IID and generated-code crosscheck retained with the source.
No CreateAction/CreateSet/IDHwpParameterSet family, alternate action or fallback.
"""
from app import native_numbering as nn
IIDS={
 'HAction':'{1A10DC8D-1155-4EE3-9EA2-1A160CDE2735}',
 'HParameterSet':'{F4D24DDE-F9FA-423C-B4FE-4BE0CF135EC5}',
 'HShapeObject':'{44D40483-8167-452B-B33A-EE0CB6993F61}',
 'HDrawImageAttr':'{DB3A0E9F-46DA-4F29-B060-42EA8959BFB7}',
 'HPictureChange':'{524AD7AC-5F82-4D44-B8A0-3326F8E49E0C}',
 'HSet':'{C64C95D7-8A60-420E-BE79-5D0D23C63316}'}
FIELDS={
 'HShapeObject':{'TextFlow':'flow16','TreatAsChar':'flag16','TextWrap':'uint16','HorzOffset':'sint32','VertOffset':'sint32'},
 'HDrawImageAttr':{'OriginalSizeX':'uint32','OriginalSizeY':'uint32','SkipLeft':'uint32','SkipRight':'uint32','SkipTop':'uint32','SkipBottom':'uint32'},
 'HPictureChange':{'PicturePath':'str','PictureEmbed':'flag16'}}

def require(calls,condition,phase):
 if not condition:
  calls.failed=True;calls.event(phase,'invalid_proof')
  raise nn.NativeNumberingReadFailure('Typed picture family proof failed at '+phase,code='picture_state_unproven')

def interface(calls,obj,name):
 # These are pywin32's documented metadata operations, not Hancom getter trials.
 # Exact C-structure/IID types are admitted before accessing or comparing them.
 import pythoncom,pywintypes
 expected=pywintypes.IID(IIDS[name])
 info=calls.call('binding.'+name+'.GetTypeInfo',lambda:obj._oleobj_.GetTypeInfo(),'object')
 attr=calls.call('binding.'+name+'.GetTypeAttr',lambda:info.GetTypeAttr(),'native_type',native_type=type(pythoncom.TYPEATTR()))
 iid=calls.call('binding.'+name+'.iid',lambda:attr.iid,'native_type',native_type=type(expected))
 require(calls,iid==expected,'binding.'+name+'.iid_matches')
 calls.event('binding.'+name,'proved',arguments={'interface':name,'iid':str(iid)})
 return obj

def action(calls,com):
 raw=calls.call('HAction',lambda:com.HAction,'object')
 return interface(calls,raw,'HAction')

class Parameters:
 def __init__(self,calls,com,replacement):
  self.calls=calls
  collection=calls.call('HParameterSet',lambda:com.HParameterSet,'object')
  interface(calls,collection,'HParameterSet')
  self.family='HPictureChange' if replacement else 'HShapeObject'
  self.root=calls.call('HParameterSet.'+self.family,lambda:getattr(collection,self.family),'object')
  interface(calls,self.root,self.family)
  self.hset=calls.call(self.family+'.HSet',lambda:self.root.HSet,'object')
  interface(calls,self.hset,'HSet')
 def image(self):
  require(self.calls,self.family=='HShapeObject','nested_image.parent_family')
  value=self.calls.call('HShapeObject.ShapeDrawImageAttr',lambda:self.root.ShapeDrawImageAttr,'object')
  return interface(self.calls,value,'HDrawImageAttr')
 def read(self,obj,family,key):
  expected=FIELDS[family][key]
  return self.calls.call('typed.'+family+'.get.'+key,lambda:getattr(obj,key),expected)
 def put(self,obj,family,key,value):
  # The declared put has VT_VOID result, not an application BOOL. setattr is
  # language assignment; successful marshalling is separately checked by a
  # typed readback. None here is never promoted to a native action success.
  expected=FIELDS[family][key]
  before=self.read(obj,family,key)
  if before==value:return
  self.calls.call('typed.'+family+'.put.'+key,lambda:setattr(obj,key,value),'void',
    arguments={'family':family,'field':key,'value':value,'contract':'IDispatch.property_put.VT_VOID'})
  actual=self.read(obj,family,key)
  require(self.calls,actual==value,'typed.'+family+'.readback.'+key)
