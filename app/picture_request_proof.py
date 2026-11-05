"""Prospective, exact request-bound UI/representation proof; analysis copies only.

Hancom ParameterSetTable_2504 pp.127-128: TextFlow 0/1/2/3.
HWPML revision 1.2 ShapeObject: BothSides/LeftOnly/RightOnly/LargestOnly.
No default inference, SDK operations, input rewriting, or generic HEAD masking.
"""
from dataclasses import dataclass
import copy, hashlib, json, re
from . import picture_preservation as xml

COORDINATES = ('List', 'Para', 'Pos')
FLOW_NAMES = {0:'BothSides', 1:'LeftOnly', 2:'RightOnly', 3:'LargestOnly'}
INTEGER = re.compile(r'(?:0|[1-9][0-9]{0,9})\Z')

def caret(root):
    """Exactly the observed HWPML/HEAD/DOCSETTING/CARETPOS schema, no alias."""
    head=xml.one(root,'head'); setting=xml.one(head,'docsetting')
    result=xml.one(setting,'caretpos')
    if sum(xml.name(e)=='caretpos' for e in root.iter()) != 1:
        raise ValueError('duplicate or foreign serialized CARETPOS')
    if sum(xml.name(e)=='docsetting' for e in root.iter()) != 1:
        raise ValueError('duplicate or foreign DOCSETTING')
    if set(result.attrib) != set(COORDINATES) or len(result):
        raise ValueError('serialized CARETPOS fields are not exact')
    values=[]
    for key in COORDINATES:
        raw=result.get(key)
        if type(raw) is not str or not INTEGER.fullmatch(raw) or int(raw)>=2**31:
            raise ValueError('serialized CARETPOS integer is not exact')
        values.append(int(raw))
    return result,tuple(values)

def position(value):
    if not any(type(value) is t for t in (tuple,list)) or len(value)!=3:
        raise ValueError('native position shape is unproved')
    if any(type(n) is not int or not 0<=n<2**31 for n in value):
        raise ValueError('native position exact integers are unproved')
    return tuple(value)

@dataclass(frozen=True)
class UIPlan:
    original: tuple
    expected: tuple
    expected_root: object
    target_id: str
    request_sha256: str
    def expected_model(self,before):
        return {**before,'root':self.expected_root}
    def verify(self,after):
        _,actual=caret(after['root'])
        if actual != self.expected:raise ValueError('post caret differs from predeclared target anchor')
        return {'contract':'selected_target_anchor','original':list(self.original),
                'expected':list(self.expected),'serialized_after':list(actual),
                'moved':self.original!=self.expected,'selection_preserved':False,
                'target_id':self.target_id,'request_sha256':self.request_sha256}

def plan(before,request,original,anchor):
    original=position(original); anchor=position(anchor)
    _,serialized=caret(before['root'])
    if serialized != original:raise ValueError('original native/serialized CARETPOS disagree')
    root=copy.deepcopy(before['root']); element,_=caret(root)
    for key,value in zip(COORDINATES,anchor):element.set(key,str(value))
    payload={k:request[k] for k in sorted(request) if k != 'label'}
    request_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return UIPlan(original,anchor,root,request['target_id'],request_hash)

@dataclass(frozen=True)
class FlowPrestate:
    observed: int
    serialized: object
    name: str
    may_materialize: bool
    def apply_expected(self,expected_shape,actual_shape):
        if self.may_materialize and 'TextFlow' in actual_shape.attrib:
            # Value comes solely from pre-Execute getter, NEVER from actual post-state.
            expected_shape.set('TextFlow',self.name)
    def report(self,final):
        serialized=final['shape'].get('TextFlow')
        return {'observed_before':self.observed,'serialized_before':self.serialized,
                'serialized_after':serialized,'materialized':self.serialized is None and serialized is not None,
                'proof':'same_observed_effective_value'}

def flow_prestate(target,request,value):
    if type(value) is not int or value not in FLOW_NAMES:
        raise ValueError('current typed TextFlow is not a documented exact enum')
    shape=xml.one(target['_element'],'shapeobject')
    aliases=[k for k in shape.attrib if k.casefold()=='textflow']
    if aliases and aliases != ['TextFlow']:raise ValueError('TextFlow accessor/serialization case conflict')
    old=shape.get('TextFlow'); label=FLOW_NAMES[value]
    if old is not None and old != label:raise ValueError('current TextFlow conflicts with raw target')
    eligible=(request['op']=='picture_layout_exact' and request['mode']=='square'
              and target['placement'].get('TreatAsChar')=='true' and old is None)
    return FlowPrestate(value,old,label,eligible)
