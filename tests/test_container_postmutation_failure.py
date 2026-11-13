"""Same create operation's post-intent read/proof failures; synthetic SDK only."""
import asyncio, json, os
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as E
from app import native_text_containers as nt, native_numbering as nn, local_cli_runtime as rt
from tests.test_shapeobject_installed_accessor import strict_owned, CREATE
from tests.test_text_container_read_diagnostic import asgi_post


def scenario(case,kind,*,session_id='custody-session-0001'):
    owned,shape,trace=strict_owned(case,session_id=session_id)
    hwp=owned.hwp;original=hwp.GetTextFile
    primary=RuntimeError('PRIVATE-post-mutation-sdk-fault')
    state={'fired':False,'primary':primary}
    def did(name):return any(len(x)>1 and x[0]=='Execute' and x[1]==name for x in hwp.calls)
    def mark():
        state['fired']=True;hwp.calls.append(('POST_MUTATION_FAULT',kind));trace.append(['FAULT',kind])
    def read(fmt,option):
        ready=(kind=='pre_read' or (did('DrawObjCreatorTextBox') and
               (kind!='after_text_read' and kind!='geometry_after_text' or did('InsertText'))))
        if ready and not option and kind not in ('healthy','state','execute_false'):
            source=original(fmt,option)
            if kind in ('after_create_read','after_text_read','pre_read'):mark();raise primary
            if kind=='parser':mark();return '<HWPML><BODY>'
            root=E.fromstring(source)
            if kind in ('geometry','geometry_after_text'):
                next(x for x in root.iter() if x.tag=='SCAMATRIX').set('E1','0')
            elif kind=='identity':
                draw=next(x for x in root.iter() if x.tag=='DRAWTEXT')
                run=next(x for x in draw.iter() if x.tag=='TEXT')
                char=next((x for x in run if x.tag=='CHAR'),None)
                if char is None:char=E.SubElement(run,'CHAR')
                char.text='unrequested'
            mark();return E.tostring(root,encoding='unicode')
        return original(fmt,option)
    hwp.GetTextFile=read
    if kind=='state':
        selected=hwp.GetSelectedPos
        def selection():
            if did('DrawObjCreatorTextBox'):mark();raise primary
            return selected()
        hwp.GetSelectedPos=selection
    if kind=='execute_false':
        execute=hwp.HAction.Execute
        def attempt(name,hset):
            if name=='DrawObjCreatorTextBox':mark();return False
            return execute(name,hset)
        hwp.HAction.Execute=attempt
    return owned,trace,state


def retained(case,owned,trace,state,result):
    out=Path(os.environ['TMPDIR']).parent/'postmutation-evidence';out.mkdir(exist_ok=True)
    data={'test':case.id(),'fired':state['fired'],'trace':trace,'calls':owned.hwp.calls,
          'actual_owning_held':owned.session._native_custody_held,'reply':result,
          'model_not_native_or_historical_cause':True}
    (out/(case.id().split('.')[-1]+'.json')).write_text(json.dumps(data,indent=2)+'\n')


def debt(case,owned,trace,state,result,*,may=True):
    case.assertTrue(state['fired'])
    case.assertFalse(result['ok'],result)
    case.assertTrue(owned.session._native_custody_held)
    step=result['steps'][0]
    case.assertTrue(step['native_custody_held'])
    case.assertIs(step['mutation_may_have_persisted'],may)
    case.assertIs(step['dirty'],may)
    case.assertIs(result['dirty'],may)
    case.assertNotIn('refusal_code',step)
    i=next(i for i,x in enumerate(owned.hwp.calls) if x[0]=='POST_MUTATION_FAULT')
    case.assertEqual(owned.hwp.calls[i+1:],[],'no SDK follow-on including restoration/cleanup')
    i=next(i for i,x in enumerate(trace) if x[0]=='FAULT')
    case.assertEqual(trace[i+1:],[])
    case.assertEqual(owned.cleanup_events(),[])
    case.assertNotIn('PRIVATE-',json.dumps(result))
    return step['failure_diagnostic']


class PostMutation(unittest.TestCase):
    def check(self,kind,site,returned='raised'):
        owned,trace,state=scenario(self,kind)
        reads=[];wrappers=[]
        create=nt._read_failure;project=rt._native_diagnostic
        def capture(*args):
            error=create(*args);reads.append(error);return error
        def after_hold(*args):
            self.assertTrue(owned.session._native_custody_held)
            if type(args[1]) is nn.NativeNumberingMutationError:wrappers.append(args[1])
            return project(*args)
        with patch.object(nt,'_read_failure',side_effect=capture),patch.object(rt,'_native_diagnostic',side_effect=after_hold):
            result,error=owned.run(CREATE)
        self.assertIsNone(error);retained(self,owned,trace,state,result)
        facts=debt(self,owned,trace,state,result)
        self.assertEqual((facts['causal_class'],facts['cause_class']),('NativeNumberingMutationError','NativeNumberingReadFailure'))
        if reads:
            self.assertTrue(wrappers)
            self.assertIs(BaseException.__dict__['__cause__'].__get__(wrappers[0]),reads[-1])
            if kind in ('after_create_read','after_text_read','state'):
                self.assertIs(BaseException.__dict__['__cause__'].__get__(reads[-1]),state['primary'])
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),(site,returned),facts)
    def test_post_execute_read_keeps_inner_cached_capsule(self):self.check('after_create_read','container.GetTextFile')
    def test_after_text_read_keeps_inner_cached_capsule(self):self.check('after_text_read','container.GetTextFile')
    def test_post_execute_parser_keeps_inner_cached_phase(self):self.check('parser','container.document.xml')
    def test_post_execute_identity_check_keeps_inner_capsule(self):self.check('identity','container.new_text_box_identity','invalid')
    def test_initial_anchor_geometry_proof_is_labelled(self):self.check('geometry','container.created_box_geometry')
    def test_after_text_anchor_geometry_proof_is_labelled(self):self.check('geometry_after_text','container.created_box_geometry.after_text')
    def test_enter_text_navigation_read_keeps_first_fault(self):self.check('state','container.GetSelectedPos')
    def test_attempted_mutator_false_never_becomes_pristine(self):self.check('execute_false','container.TextBox.Execute','false')
    def test_healthy_complete_create_is_still_eligible(self):
        owned,trace,state=scenario(self,'healthy');result,error=owned.run(CREATE)
        self.assertIsNone(error);self.assertTrue(result['ok'],result)
        self.assertFalse(state['fired']);self.assertFalse(owned.session._native_custody_held)
        self.assertTrue(result['dirty'])
    def test_pre_edit_read_does_not_acquire_mutation_debt(self):
        owned,trace,state=scenario(self,'pre_read');result,error=owned.run(CREATE)
        self.assertIsNone(error);facts=debt(self,owned,trace,state,result,may=False)
        self.assertEqual(facts['causal_class'],'NativeNumberingReadFailure')
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),('container.GetTextFile','raised'))


class CallbackSafety(unittest.TestCase):
    def check(self,kind):
        from tests.test_diagnostic_no_callbacks import CallbackDict, ForeignKey
        events=[];owned,trace,state=scenario(self,'after_create_read')
        original=nt._read_failure;inner=[]
        def poison(*args):
            error=original(*args);inner.append(error)
            if kind=='mapping':
                mapping=CallbackDict(events);dict.__setitem__(mapping,'native_site','container.GetTextFile')
                error.__dict__=mapping
            else:
                key=ForeignKey('native_site',events)
                error.__dict__={key:'container.GetTextFile','native_return':'raised'};events.clear()
            return error
        with patch.object(nt,'_read_failure',side_effect=poison):result,error=owned.run(CREATE)
        self.assertIsNone(error);facts=debt(self,owned,trace,state,result)
        self.assertTrue(inner);self.assertEqual(events,[])
        self.assertNotIn('native_site',facts)
    def test_hostile_dictionary_has_no_callbacks(self):self.check('mapping')
    def test_foreign_collision_key_has_no_callbacks(self):self.check('key')


class PostMutationHTTP(unittest.TestCase):
    def test_real_http_keeps_dirty_hold_and_inner_read_label(self):
        owned,trace,state=scenario(self,'after_create_read')
        status,result=asyncio.run(asgi_post(owned.service,CREATE))
        self.assertEqual(status,200);retained(self,owned,trace,state,result)
        facts=debt(self,owned,trace,state,result)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),('container.GetTextFile','raised'))


class FutureSettlement(unittest.TestCase):
    def test_postmutation_first_cause_and_future_survive_reporter_failure(self):
        owned,trace,state=scenario(self,'after_create_read')
        def handler(handle):return nt.execute(handle.hwp,CREATE,on_mutating=handle.mark_mutating_intent)
        with patch.object(rt,'record_failure',side_effect=MemoryError('PRIVATE-reporter')):
            with self.assertRaises(rt.LocalCliNativeCustodyHeldError) as raised:
                owned.manager.execute(session_id=owned.sid,command_name='command-bundle',handler=handler,timeout=5)
        self.assertTrue(state['fired']);self.assertTrue(owned.session._native_custody_held)
        mutation=BaseException.__dict__['__cause__'].__get__(raised.exception)
        self.assertIs(type(mutation),nn.NativeNumberingMutationError)
        read=BaseException.__dict__['__cause__'].__get__(mutation)
        self.assertIs(type(read),nn.NativeNumberingReadFailure)
        self.assertIs(BaseException.__dict__['__cause__'].__get__(read),state['primary'])
        self.assertEqual(owned.cleanup_events(),[])
