"""Read-side, complete XML allowed-delta checks. Never writes XML to Hancom."""
import copy
import xml.etree.ElementTree as ET

def name(e):return e.tag.rsplit('}',1)[-1].lower()
def one(e,tag):
    found=[x for x in e if name(x)==tag]
    if len(found)!=1:raise ValueError('exact picture child is unproved: '+tag)
    return found[0]
def canon(e):return ET.canonicalize(ET.tostring(e,encoding='unicode'),strip_text=False)
def set_attr(e,key,value):
    keys=[k for k in e.attrib if k.casefold()==key.casefold()]
    if len(keys)!=1:raise ValueError('exact picture attribute is unproved: '+key)
    e.set(keys[0],str(value))
def attr(e,key):
    values=[v for k,v in e.attrib.items() if k.casefold()==key.casefold()]
    if len(values)!=1:raise ValueError('exact media attribute is unproved: '+key)
    return values[0]
def parent_of(root,child):
    found=[p for p in root.iter() if child in list(p)]
    if len(found)!=1:raise ValueError('exact media parent is unproved')
    return found[0]

def prove(before,after,target,final,request,expected_crop,*,flow_prestate=None,legacy_media=True):
    """Only request-derived fields may differ; all other target nodes/attrs/text remain.

    Replacement may reuse an unchanged embedded asset or allocate exactly one
    appended embedded PNG member. Shared/original members are never rewritten.
    Other native renewal or crop-transform representations remain unqualified.

    ``legacy_media=False`` is chosen only BEFORE any effect by the native HWPX
    package strategy, whose complete package comparison is the media/HEAD/TAIL
    authority. It is never a fallback after this legacy comparison failed, and
    it does not certify HWPML TAIL/BINDATA (including Size) as unchanged.
    """
    expected=copy.deepcopy(target['_element']);actual=final['_element'];op=request['op']
    shape=one(expected,'shapeobject')
    if op=='picture_replace_exact':
        set_attr(one(expected,'image'),'BinItem',attr(one(actual,'image'),'BinItem'))
    elif op=='picture_crop_exact':
        clip=one(expected,'imageclip')
        for key,value in expected_crop.items():set_attr(clip,key,value)
    elif op=='picture_layout_exact':
        position=one(shape,'position')
        set_attr(position,'TreatAsChar','true' if request['mode']=='inline' else 'false')
        # Request-derived: square puts TreatAsChar=0 and TextWrap=0 (Square);
        # inline puts TreatAsChar=1 only, so its TextWrap stays unchanged.
        if request['mode']=='square':set_attr(shape,'TextWrap','Square')
        if flow_prestate is not None:
            flow_prestate.apply_expected(shape,one(actual,'shapeobject'))
        for public,native in (('horizontal_offset_mm','HorzOffset'),('vertical_offset_mm','VertOffset')):
            if public in request:set_attr(position,native,round(request[public]*7200/25.4))
    else:raise ValueError('unknown picture operation')
    if canon(expected)!=canon(actual):raise ValueError('unrequested complete target picture delta')
    if not legacy_media:return
    bh=one(before['root'],'head');ah=one(after['root'],'head')
    bt=one(before['root'],'tail');at=one(after['root'],'tail')
    if op!='picture_replace_exact':
        if canon(bh)!=canon(ah) or canon(bt)!=canon(at):raise ValueError('unrequested head/tail/media delta')
        return
    old_items=[e for e in bh.iter() if name(e)=='binitem'];new_items=[e for e in ah.iter() if name(e)=='binitem']
    old_data=[e for e in bt.iter() if name(e)=='bindata'];new_data=[e for e in at.iter() if name(e)=='bindata']
    if len(new_items)==len(old_items) and len(new_data)==len(old_data):
        if canon(bh)!=canon(ah) or canon(bt)!=canon(at):raise ValueError('existing replacement asset was rewritten')
        return
    if len(new_items)!=len(old_items)+1 or len(new_data)!=len(old_data)+1:raise ValueError('unqualified replacement media allocation')
    if any(canon(a)!=canon(b) for a,b in zip(old_items,new_items)) or any(canon(a)!=canon(b) for a,b in zip(old_data,new_data)):
        raise ValueError('original/shared media changed')
    item=new_items[-1];data=new_data[-1]
    if attr(item,'Type')!='Embedding' or attr(item,'Format').lower()!='png':raise ValueError('replacement allocation is not embedded PNG')
    if len(final['media'])!=1 or attr(item,'BinData')!=final['media'][0]['member_id'] or attr(data,'Id')!=final['media'][0]['member_id'] or str(len(new_items))!=final['media'][0]['bin_item']:
        raise ValueError('new asset is not exactly target-bound')
    eh=copy.deepcopy(bh);et=copy.deepcopy(bt)
    ei=[e for e in eh.iter() if name(e)=='binitem'];ed=[e for e in et.iter() if name(e)=='bindata']
    if not ei or not ed:raise ValueError('original media allocation parent is unproved')
    ip=parent_of(eh,ei[-1]);ip.append(copy.deepcopy(item))
    if 'Count' in ip.attrib:ip.set('Count',str(len(new_items)))
    dp=parent_of(et,ed[-1]);dp.append(copy.deepcopy(data))
    if 'Count' in dp.attrib:dp.set('Count',str(len(new_data)))
    if canon(eh)!=canon(ah) or canon(et)!=canon(at):raise ValueError('unrequested replacement head/tail delta')
