"""Explicit SYNTHETIC legacy-append diagnostic producer model, NOT nt.execute.
The corrected product never doubles text. This fixture retains regression
coverage for the unchanged private append comparison/capture producer without
pretending that the retired append consumer is the current product algorithm.
SDK boundaries are ContainersHwp fakes; owning service/runtime/journal and all
binding/owner/capture/first-fault methods below are the actual source functions.
"""
from app import native_text_containers as nt

def legacy_append_model(hwp,step,*,on_mutating=None,record_binding_precomparison=None):
 request=nt.validate_step(step)
 if request['op']!='text_container_text_exact':raise AssertionError('legacy diagnostic fixture only')
 native=nt._Native(hwp,record_binding_precomparison=record_binding_precomparison)
 before=native.read();item=nt._target(before,request);entry=native.witness();original=entry[0]
 if not item['editable'] or item['text']!=request['expected_text']:raise AssertionError('fixture admission')
 try:
  lid=nt._locate(native,before,item);atoms=item['_rows'][0]['atoms'];first=next(i for i,a in enumerate(atoms)if'char'in a);start=sum(a['width']for a in atoms[:first]);last=sum(a['width']for a in atoms)
  native.select(lid,start,last,item);native.at((lid,0,last));nt._require_owner(native,item,'insertion_owner');native.read_unchanged(before,'pre_insert_document_drift')
  if native.state()!=((lid,0,last),()):native.hold('pre_insert_document_drift')
  native.insert(request['new_text'],on_mutating);item['_binding_text']=item['text']+request['new_text'];native.select(lid,start,last,item)
  action=native.call('HAction.delete',lambda:native.com.HAction,'object');native.call('Delete',lambda:action.Run('Delete'),'true');post=native.witness();after=native.read();later=nt._target(after,{**request,'expected_hash':next(x['hash']for x in after['containers']if x['target_id']==item['target_id'])})
  if later['text']!=request['new_text']or not later['editable']or nt._signature(before,mask=item['_rows'][0]['element'])!=nt._signature(after,mask=later['_rows'][0]['element']):native.hold('complete_document_preservation')
  if native.witness()!=post:native.hold('read_purity')
  later['_native_binding']=(after,item['_native_binding'][1]);nt._require_owner(native,later,'insertion_owner');native.at(original)
  return {'read_only':False,'container':nt.public(later),'result':{'changed':True,'status':'synthetic_legacy_diagnostic_fixture_only'},'warnings':['Synthetic retired-consumer model; not current product/native acceptance.']}
 except nt.nn.NativeNumberingReadFailure as exc:
  if native.issued:raise nt._mutation_failure('Synthetic legacy diagnostic first fault; no next SDK.',exc)from exc
  raise
