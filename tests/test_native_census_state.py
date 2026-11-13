"""Full-census state boundary through engine, service and owning composition.
Control/Anchor/link use the strict manual interface model in the sibling module.
These model scenarios are not native SDK or rendered-document evidence.
"""
import json,os,unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from app import native_text_containers as nt
from app.native_numbering import NativeNumberingReadFailure
from tests.native_census_state_review_models import Control,link
from tests.native_text_containers_fakes import ContainersHwp
from tests.test_native_container_identity import setup
from tests.test_native_constructor_postcreate import actual_owned,CREATE

NATIVE='900000001'
NAV={'SetPos','set_pos','SelectText','select_text','Execute'}

def manual(case,phase=1,effect='caret',*,kind='text_box',create=False,session_id='custody-session-0001'):
 if create:
  own,trace,state=actual_owned(case,session_id=session_id);request=CREATE
  # Actual constructor fixture's serialized ID is DATA, not a native selector.
  from tests.test_native_constructor_postcreate import native_root,owner
  own.hwp.native_ids={owner(native_root()).find('SHAPEOBJECT').get('InstId'):NATIVE}
  point=(0,0,238);target='gso'
 else:
  own,trace,request=setup(case,kind,session_id=session_id);state=None
  point=(0,1,4);target='fn' if kind=='footnote' else 'gso'
 h=own.hwp;events=[];count=[0]
 def change():
  h.calls.append(('UNREQUESTED_CENSUS_STATE',count[0],effect))
  if effect=='caret':h.cursor=(0,2,0) if not create else (0,0,0)
  elif effect=='selection':h.selection=(0,0,0,0,0,1)
  else:raise AssertionError('Unknown declared model effect')
 def head(_h):
  count[0]+=1;h.calls.append(('CENSUS_BEGIN',count[0]))
  return link([Control('secd',(0,0,0),'',events),Control(target,point,NATIVE,events,effect=change if count[0]==phase else None)])
 return own,request,state,events,head

@contextmanager
def watched(h,head):
 original=nt._Native.hold
 def hold(native,site,returned='invalid'):
  h.calls.append(('TECHNICAL_HOLD',site,returned))
  return original(native,site,returned)
 with patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt._Native,'hold',hold):yield

def save(case,label,own,result,events):
 p=Path(os.environ['TMPDIR']).parent/'census-state-evidence';p.mkdir(exist_ok=True)
 (p/(case.id().split('.')[-1]+'-'+label+'.json')).write_text(json.dumps({'test':case.id(),'label':label,'result':result,
  'calls':own.hwp.calls,'control_events':events,'owning_HOLD':own.session._native_custody_held,'cleanup':own.cleanup_events(),
  'scope':'Source/service/owner/HTTP/MCP SDK models only; no physical native fault or historical box11 cause.'},indent=1))

def check_held(case,own,result,error,events,*,created=False,label='case'):
 save(case,label,own,result,events)
 case.assertIsNone(error);case.assertFalse(result['ok'],result);case.assertTrue(own.session._native_custody_held)
 calls=own.hwp.calls
 marked=next(i for i,c in enumerate(calls) if c[0]=='UNREQUESTED_CENSUS_STATE')
 case.assertFalse(any(c[0] in NAV for c in calls[marked+1:]),calls[marked+1:])
 held=next(i for i,c in enumerate(calls) if c[0]=='TECHNICAL_HOLD')
 case.assertEqual(calls[held][1],'binding.state')
 # Healthy outgoing state reads BEFORE HOLD are required, not forbidden follow-on.
 case.assertEqual([c[0] for c in calls[marked+1:held]],['GetPos','GetSelectedPos','GetPos'])
 case.assertEqual(calls[held+1:],[])
 case.assertEqual(own.cleanup_events(),[])
 step=result['steps'][0];case.assertEqual(step['dirty'],created)
 case.assertEqual(step['mutation_may_have_persisted'],created)
 case.assertEqual([c[1] for c in own.hwp.executes()],['DrawObjCreatorTextBox'] if created else [])

class CensusState(unittest.TestCase):
 def run_drift(self,phase,*,create=False):
  for effect in ('caret','selection'):
   with self.subTest(phase=phase,effect=effect):
    own,request,state,events,head=manual(self,phase,effect,create=create)
    with watched(own.hwp,head):result,error=own.run(request)
    check_held(self,own,result,error,events,created=create,label=str(phase)+'-'+effect)
    if create:
     self.assertTrue(state['created']);self.assertNotIn(CREATE['text'],''.join(state['root'].itertext()))
 def test_initial_census_caret_and_selection_hold_before_navigation(self):self.run_drift(1)
 def test_scan_refresh_caret_and_selection_hold_before_navigation(self):self.run_drift(2)
 def test_later_require_owner_census_retains_the_same_central_guard(self):self.run_drift(3)
 def test_post_create_census_drift_preserves_dirty_unknown_debt(self):self.run_drift(1,create=True)
 def test_healthy_manual_all_target_pairs_and_constructor(self):
  for kind in ('text_box','caption','footnote','constructor'):
   with self.subTest(kind=kind):
    own,request,state,events,head=manual(self,None,create=kind=='constructor',kind=kind)
    with watched(own.hwp,head):result,error=own.run(request)
    save(self,kind,own,result,events);self.assertIsNone(error);self.assertTrue(result['ok'],result)
    self.assertFalse(own.session._native_custody_held)
    self.assertFalse(any(e[0] in ('GetAnchorPos','GetCtrlInstID') and e[1]=='secd' for e in events))
 def test_direct_healthy_census_preserves_nonempty_selection(self):
  own,request,state,events,head=manual(self,None);h=own.hwp
  model=nt.parse(h.GetTextFile('HWPML2X',''));item=model['containers'][0]
  h.cursor=(2,0,4);h.selection=(2,0,0,2,0,4);native=nt._Native(h)
  with watched(h,head):
   before=native.state();binding=nt._binding(native,model,item);after=native.state()
  self.assertEqual(before,((2,0,4),(2,0,0,2,0,4)));self.assertEqual(after,before)
  self.assertEqual(binding,('gso',(0,1,4),NATIVE,(((0,1,4),NATIVE),)))
  self.assertFalse(any(c[0] in NAV for c in h.calls));self.assertFalse(native.failed)
 def test_getter_fault_or_invalid_return_never_issues_post_state_sdk(self):
  for mode in ('raises','invalid'):
   with self.subTest(mode=mode):
    own,request,state,events,head=manual(self,None);h=own.hwp;original=Control.GetAnchorPos
    primary=RuntimeError('immutable modeled first getter fault')
    def anchor(ctrl,which):
     if ctrl.kind=='secd':return original(ctrl,which)
     h.calls.append(('FIRST_GETTER_FAULT',mode))
     if mode=='raises':raise primary
     return None
    with watched(h,head),patch.object(Control,'GetAnchorPos',anchor):result,error=own.run(request)
    save(self,mode,own,result,events);self.assertIsNone(error);self.assertFalse(result['ok'])
    self.assertTrue(own.session._native_custody_held);self.assertEqual(h.executes(),[])
    marker=next(i for i,c in enumerate(h.calls) if c[0]=='FIRST_GETTER_FAULT')
    self.assertEqual(h.calls[marker+1:],[] if mode=='raises' else [('TECHNICAL_HOLD','control.GetAnchorPos','none')])
    self.assertEqual(own.cleanup_events(),[])
 def test_incoming_and_outgoing_state_capture_invalidity_stops_first(self):
  for at in (1,3):
   with self.subTest(at=at):
    own,request,state,events,head=manual(self,None);h=own.hwp
    model=nt.parse(h.GetTextFile('HWPML2X',''));item=model['containers'][0];count=[0]
    def pos():
     count[0]+=1;h.calls.append(('STATE_CAPTURE',count[0]))
     return (True,0,0) if count[0]==at else (0,0,0)
    h.GetPos=pos;h.calls.clear();native=nt._Native(h)
    with watched(h,head),self.assertRaises(NativeNumberingReadFailure):nt._binding(native,model,item)
    self.assertTrue(native.failed)
    self.assertEqual(h.calls[-2:],[('STATE_CAPTURE',at),('TECHNICAL_HOLD','GetPos','tuple')])
    self.assertEqual(sum(c[0]=='CENSUS_BEGIN' for c in h.calls),0 if at==1 else 1)
    self.assertFalse(any(c[0] in NAV for c in h.calls))
