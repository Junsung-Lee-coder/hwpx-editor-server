"""Explicit public picture facts, separate from unchanged private journal policy."""
import re
from typing import Any

MAX_PICTURES=1000
_NUM=re.compile(r'^-?[0-9]{1,16}(?:\.[0-9]{1,12})?$')
_HASH=re.compile(r'^(?:sha256:)?[0-9a-f]{24}(?:[0-9a-f]{40})?$')
_ID=re.compile(r'^[0-9]{1,20}$')
_TARGET=re.compile(r'^picture:[0-9]{1,20}$')
_NUM_KEYS=frozenset(('Width','Height','HorzOffset','VertOffset','InstId','InstID','ZOrder','GroupLevel','OriWidth','OriHeight','XPos','YPos','X0','X1','X2','X3','Y0','Y1','Y2','Y3','Left','Top','Right','Bottom'))
_BOOL_KEYS=frozenset(('Protect','AffectLSpacing','AllowOverlap','FlowWithText','HoldAnchorAndSO','TreatAsChar','Lock','HorzFlip','VertFlip'))
_ENUMS={
 'TextFlow':frozenset(('BothSides','LeftOnly','RightOnly','LargestOnly')),
 'TextWrap':frozenset(('Square','TopAndBottom','BehindText','InFrontOfText','Behind','InFront')),
 'NumberingType':frozenset(('None','Picture','Figure')),
 'WidthRelTo':frozenset(('Absolute','Paper','Page','Column','Para')),
 'HeightRelTo':frozenset(('Absolute','Paper','Page','Column','Para')),
 'HorzRelTo':frozenset(('Paper','Page','Column','Para','Paragraph')),
 'VertRelTo':frozenset(('Paper','Page','Column','Para','Paragraph')),
 'HorzAlign':frozenset(('Left','Center','Right','Inside','Outside')),
 'VertAlign':frozenset(('Top','Center','Bottom','Inside','Outside')),
}
def plain(raw):
 if type(raw) is not dict or len(raw)>256:return {}
 return {k:v for k,v in dict.items(raw) if type(k) is str}

def attrs(raw):
 if type(raw) is not dict:return None
 raw=plain(raw)
 out={}
 for key,value in dict.items(raw):
  if type(key) is not str or type(value) is not str:continue
  if (key in _NUM_KEYS and _NUM.fullmatch(value)) or (key in _BOOL_KEYS and value in ('true','false')) or (key in _ENUMS and value in _ENUMS[key]):out[key]=value
 return out

def record(raw):
 if type(raw) is not dict:return None
 raw=plain(raw)
 out={}
 for key,pattern in (('target_id',_TARGET),('hash',_HASH),('native_shapeobject_instid',_ID),('native_component_instid',_ID)):
  value=dict.get(raw,key);out[key]=value if type(value) is str and pattern.fullmatch(value) else None
 context=dict.get(raw,'context');out['context']=context if type(context) is str and context in ('body','table_cell','group','header_footer','other_nested') else 'unproved'
 for key in ('frame','placement','shape','component','image_rect','crop','source_dimensions'):out[key]=attrs(dict.get(raw,key))
 media=dict.get(raw,'media');out['media']=[]
 if type(media) is list:
  for value in media[:8]:
   if type(value) is not dict:continue
   value=plain(value)
   item={}
   for key,pattern in (('bin_item',_ID),('member_id',_ID),('payload_sha256',_HASH)):
    v=dict.get(value,key)
    if type(v) is str and pattern.fullmatch(v):item[key]=v
   out['media'].append(item)
 caption=plain(dict.get(raw,'caption'))
 digest=dict.get(caption,'digest');out['caption']={'present':dict.get(caption,'present') is True,'digest':digest if type(digest) is str and _HASH.fullmatch(digest) else None,'text':None}
 out['supported']=dict.get(raw,'supported') is True
 reasons=dict.get(raw,'refusal_reasons');out['refusal_reasons']=[v for v in reasons[:32] if type(v) is str and re.fullmatch(r'picture_[a-z_]{1,64}',v)] if type(reasons) is list else []
 return out

_SHA64=re.compile(r'^[0-9a-f]{64}$')
_PROFILE_ID=re.compile(r'^[A-Za-z0-9._/-]{1,80}$')
_MEMBER=re.compile(r'^Contents/section(?:0|[1-9][0-9]{0,3})\.xml$')
def saved_proof(raw):
 """Typed private-copy package proof facts; no paths, member bytes or document text."""
 raw=plain(raw)
 if dict.get(raw,'strategy')!='native_hwpx_package':return None
 out={'strategy':'native_hwpx_package'}
 for key in ('profile_sha256','before_package_sha256','after_package_sha256','evidence_sha256'):
  v=dict.get(raw,key)
  if type(v) is str and _SHA64.fullmatch(v):out[key]=v
 v=dict.get(raw,'profile_id')
 if type(v) is str and _PROFILE_ID.fullmatch(v):out['profile_id']=v
 for key,values in (('qualification',('qualified','unqualified')),('replacement',('none','reused','allocated')),
   ('legacy_hwpml_bindata_size',('unknown_unqualified_not_compared',)),('source',('operator_configured_file',))):
  v=dict.get(raw,key)
  if type(v) is str and v in values:out[key]=v
 for key in ('members','byte_identical','infoset_compared','regenerated_not_compared','added_media'):
  v=dict.get(raw,key)
  if type(v) is int and 0<=v<2**31:out[key]=v
 for key in ('native_qualified','private_copy_is_user_save','logical_debt_discharged'):
  v=dict.get(raw,key)
  if type(v) is bool:out[key]=v
 from app.hwpx_saved_proof import EVIDENCE_NAMES,REFLOW_CONTRACT,REFLOW_FIELDS
 for key in ('evidence_used','evidence_not_observed'):
  v=dict.get(raw,key)
  if type(v) is list and all(type(x) is str and x in EVIDENCE_NAMES for x in v):out[key]=list(v)
 reflow=plain(dict.get(raw,'derived_line_reflow'))
 if dict.get(reflow,'contract')==REFLOW_CONTRACT and dict.get(reflow,'trigger')=='picture_layout_exact':
  # Derived line reflow, reported apart from the unchanged formatting; typed values only.
  projected={'contract':REFLOW_CONTRACT,'trigger':'picture_layout_exact'}
  v=dict.get(reflow,'section_member')
  if type(v) is str and _MEMBER.fullmatch(v):projected['section_member']=v
  for key in ('paragraph_child_index','lines','changed_lines'):
   v=dict.get(reflow,key)
   if type(v) is int and 0<=v<2**31:projected[key]=v
  v=dict.get(reflow,'paragraph_id')
  if v is None or (type(v) is str and _ID.fullmatch(v)):projected['paragraph_id']=v
  v=dict.get(reflow,'changed_fields')
  if type(v) is list and all(type(x) is str and x in REFLOW_FIELDS for x in v):projected['changed_fields']=list(v)
  out['derived_line_reflow']=projected
 link=plain(dict.get(raw,'bridge'))
 if dict.get(link,'method')=='owpml_physical_ordinal/v1':
  bridge={'method':'owpml_physical_ordinal/v1','identity_namespaces':'HWPML SHAPEOBJECT InstId and HWPX hp:pic ids are not compared'}
  for key in ('ordinal','pictures'):
   v=dict.get(link,key)
   if type(v) is int and 0<=v<=MAX_PICTURES*4:bridge[key]=v
  v=dict.get(link,'section_member')
  if type(v) is str and _MEMBER.fullmatch(v):bridge['section_member']=v
  out['bridge']=bridge
 epoch=plain(dict.get(raw,'epoch'))
 if type(dict.get(epoch,'sequence')) is int and 0<=epoch['sequence']<2**31:out['epoch_sequence']=epoch['sequence']
 return out

def public_result(raw):
 if type(raw) is not dict:return {}
 raw=plain(raw)
 out={}
 for key in ('ok','read_only','applied','changed','history_qualified'):
  value=dict.get(raw,key)
  if type(value) is bool:out[key]=value
 for key in ('native_actions_issued','undo_native_actions'):
  value=dict.get(raw,key)
  if value is None or (type(value) is int and 0<=value<=1000):out[key]=value
 for key,pattern in (('target_id',_TARGET),('hash',_HASH),('native_GetCtrlInstID',_ID)):
  value=dict.get(raw,key)
  if type(value) is str and pattern.fullmatch(value):out[key]=value
 for key in ('frame','placement','crop'):
  if key in raw:out[key]=attrs(dict.get(raw,key))
 if 'media' in raw:
  picture=record(raw)
  out['media']=picture['media'] if picture is not None else []
 ui=plain(dict.get(raw,'ui_position'))
 contract=dict.get(ui,'contract')
 if type(contract) is str and contract=='selected_target_anchor':
  projected: dict[str,Any]={'contract':'selected_target_anchor'}
  for key in ('original','expected','serialized_after'):
   value=dict.get(ui,key)
   if type(value) is list and len(value)==3 and all(type(v) is int and 0<=v<2**31 for v in value):
    projected[key]=list(value)
  for key in ('moved','selection_preserved'):
   value=dict.get(ui,key)
   if type(value) is bool:projected[key]=value
  for key,pattern in (('target_id',_TARGET),('request_sha256',re.compile(r'^[0-9a-f]{64}$'))):
   value=dict.get(ui,key)
   if type(value) is str and pattern.fullmatch(value):projected[key]=value
  out['ui_position']=projected
 flow=plain(dict.get(raw,'effective_text_flow'))
 flow_proof=dict.get(flow,'proof')
 if type(flow_proof) is str and flow_proof=='same_observed_effective_value':
  projected={'proof':'same_observed_effective_value'}
  value=dict.get(flow,'observed_before')
  if type(value) is int and value in (0,1,2,3):projected['observed_before']=value
  for key in ('serialized_before','serialized_after'):
   value=dict.get(flow,key)
   if value is None or (type(value) is str and value in _ENUMS['TextFlow']):projected[key]=value
  value=dict.get(flow,'materialized')
  if type(value) is bool:projected['materialized']=value
  out['effective_text_flow']=projected
 preservation=plain(dict.get(raw,'preservation'))
 if type(preservation) is dict:
  out['preservation']={k:dict.get(preservation,k) for k in ('unrelated_body_styles_controls','other_picture_payloads_references','frame','caption','complete_target_allowed_delta','head_tail_media') if type(dict.get(preservation,k)) is bool}
 if type(dict.get(preservation,'complete_hwpx_package')) is bool and 'preservation' in out:
  out['preservation']['complete_hwpx_package']=preservation['complete_hwpx_package']
 for key,values in (
  ('reason',('already_requested_state','same_decoded_pixels')),
  ('history_reason',('native_undo_redo_not_qualified','no_native_edit','profile_declared_history')),
  ('native_action',('PictureChange','ShapeObjDialog')),
  ('preservation_strategy',('legacy_hwpml_strict','native_hwpx_package'))):
  v=dict.get(raw,key)
  if type(v) is str and v in values:out[key]=v
 saved=saved_proof(dict.get(raw,'saved_proof'))
 if saved is not None:out['saved_proof']=saved
 undo=plain(dict.get(raw,'undo'))
 if dict.get(undo,'hwpx_undo')=='profile_declared':
  out['history']={'basis':'profile_declared_hwpx_package_states','qualified':dict.get(raw,'history_qualified') is True,
   **{k:undo[k] for k in ('undo_native_steps','redo_native_steps') if type(dict.get(undo,k)) is int and 1<=undo[k]<=4},
   **{k:undo[k] for k in ('pre_state_sha256','post_state_sha256') if type(dict.get(undo,k)) is str and _SHA64.fullmatch(undo[k])}}
 pictures=dict.get(raw,'pictures')
 if type(pictures) is list:
  out['schema_version']='local-cli/picture-inventory/v1'
  out['pictures']=[p for value in pictures[:MAX_PICTURES] if (p:=record(value)) is not None]
  out['total_count']=len(pictures);out['returned_count']=len(out['pictures'])
  out['complete']=dict.get(raw,'complete') is True and len(pictures)==len(out['pictures'])
  out['public_projection']='bounded_safe_picture_facts/v1'
  out['identity_namespace']='HWPML SHAPEOBJECT InstId; native selection requires fresh GetCtrlInstID mapping'
  out['edit_contract']={'implemented_contexts':['body_without_native_caption'],'replacement':'regular backend-host PNG, same decoded aspect ratio','crop':'normalized source bounds 0..1, same frame and payload; rendered geometry not yet qualified','layout_modes':['inline','square'],'native_edit_qualified':False,'native_history_qualified':False,'maximum_pictures':MAX_PICTURES,'over_bound':'clean picture_inventory_limit refusal; no silently missing bindings','caption_text':'not exposed; association uses presence and digest','preservation':'legacy HWPML strict comparison by default; complete native HWPX private-copy package proof only with an explicitly configured pinned profile; legacy HWPML BINDATA Size is not certified'}
  out['qualification']='structural inventory, not native mutation qualification'
 return out
