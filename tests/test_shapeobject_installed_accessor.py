"""Installed-name DATA-backed shape fixture and real caller regressions.

The generic HSet item bag is NOT an attribute namespace: accepting SetItem does
not invent a generated accessor. This models the reached old getter mismatch,
not box08's unretained exception. No case folding/default values/SDK execution.
"""
import json, os
from pathlib import Path
from types import MethodType
import unittest
from unittest.mock import patch
from app import local_cli_runtime as rt, native_numbering as nn
from app.native_text_containers import _Native
from app.local_cli_service import LocalCliService
from tests.native_text_containers_fakes import ContainersHwp, Layout, Shape
from tests.test_native_numbering_runtime_custody import Owned
from tests.test_text_container_read_diagnostic import CREATE, asgi_post

CONTRACT=json.loads((Path(__file__).parent/'fixtures/hshapeobject_requested_members.json').read_text())
MEMBERS={m['name']:m for m in CONTRACT['members']}
PIN='f9ed176b04b25ea8eb3e837aba4825d3d3e73ce7484350ddf4b6678005f5a3fc'

class StrictShape:
    """Only the six independently generated getters/putters; synthetic transport."""
    def __init__(self,trace,faults=None,calls=None):
        object.__setattr__(self,'_trace',trace)
        object.__setattr__(self,'_calls',calls)
        object.__setattr__(self,'_values',{})
        object.__setattr__(self,'_faults',faults or {})
        object.__setattr__(self,'ShapeDrawLayOut',Layout())
        object.__setattr__(self,'HSet',self)  # existing portable family seam, not a native IID claim
    def __getattribute__(self,name):
        if name in ('HSet','ShapeDrawLayOut','SetItem'):
            object.__getattribute__(self,'_trace').append(['parameter_member',name])
        return object.__getattribute__(self,name)
    def _fault(self,reason,name):
        self._trace.append(['FAULT',reason,name])
        if self._calls is not None:self._calls.append(('STRICT_FIELD_FAULT',name))
    def SetItem(self,name,value):
        # HSet.SetItem is BSTR/VARIANT/VOID, not typed-property assignment.
        self._trace.append(['set_item',name,value])
        self._values[name]=value
        return None
    def __getattr__(self,name):
        if name not in MEMBERS:
            self._fault('missing_generated_member',name)
            raise AttributeError('No such generated member')
        self._trace.append(['get',name])
        if name in self._faults:
            self._fault('injected_read',name)
            value=self._faults[name]
            if isinstance(value,Exception):raise value
            return value
        # Deliberately no dynamic/default value or differently cased alias.
        return self._values[name]
    def __setattr__(self,name,value):
        if name not in MEMBERS:raise AttributeError('No such generated putter')
        vt=MEMBERS[name]['getter_vartype']
        valid=type(value) is int and ((vt==18 and 0<=value<2**16) or (vt==3 and -2**31<=value<2**31))
        if not valid:raise TypeError('outside metadata representation')
        self._trace.append(['property_put',name,value])
        self._values[name]=value

class RawTrace:
    def __init__(self,hwp,trace):self.inner,self.trace=hwp,trace
    def __getattr__(self,name):
        self.trace.append(['sdk',name])
        return getattr(self.inner,name)


def strict_owned(case,*,faults=None,session_id='custody-session-0001'):
    trace=[];hwp=ContainersHwp();shape=StrictShape(trace,faults,hwp.calls)
    hwp.HParameterSet.HShapeObject=shape
    hwp.hwp=RawTrace(hwp,trace)
    owned=Owned(case,hwp,real_execute_live=True,session_id=session_id)
    hwp.cursor=(0,1,0)
    owned.service._public_bundle_steps=MethodType(LocalCliService._public_bundle_steps,owned.service)
    return owned,shape,trace


def retain(case,owned,trace,result):
    p=Path(os.environ['TMPDIR']).parent/'strict-accessor-evidence'
    p.mkdir(exist_ok=True)
    data={'test':case.id(),'trace':trace,'reply':result,'executed_actions':owned.hwp.executes(),
          'actual_owning_held':owned.session._native_custody_held,'metadata_authority':PIN,
          'scope':'model SDK; real Python service/manager/owning thread and public projection; not historical native cause'}
    (p/(case.id().split('.')[-1]+'.json')).write_text(json.dumps(data,indent=2)+'\n')
    return data


def assert_read_hold(case,owned,trace,result):
    case.assertFalse(result['ok'],result)
    case.assertTrue(owned.session._native_custody_held)
    step=result['steps'][0]
    case.assertTrue(step['native_custody_held'])
    case.assertFalse(step['mutation_may_have_persisted'])
    case.assertFalse(step['dirty'])
    case.assertNotIn('refusal_code',step)
    case.assertEqual(owned.hwp.executes(),[])
    index=next(i for i,v in enumerate(trace) if v[0]=='FAULT')
    case.assertEqual(trace[index+1:],[],'no next SDK access after first failed getter')
    index=next(i for i,v in enumerate(owned.hwp.calls) if v[0]=='STRICT_FIELD_FAULT')
    case.assertEqual(owned.hwp.calls[index+1:],[],'no SDK method after first failed getter')
    case.assertEqual(owned.cleanup_events(),[])
    return step['failure_diagnostic']


class InstalledAccessor(unittest.TestCase):
    def test_complete_requested_member_audit_is_bound_to_independent_authority(self):
        self.assertEqual(CONTRACT['independent_full_generated_sha256'],PIN)
        self.assertFalse(CONTRACT['generated_file_executed'])
        self.assertEqual({n:m['getter_vartype'] for n,m in MEMBERS.items()},
                         {'WidthRelTo':18,'HeightRelTo':18,'Width':3,'Height':3,'TreatAsChar':18,'AdjustTextbox':18})
        for m in MEMBERS.values():
            self.assertEqual(m['getter']['ast'][0],m['putter']['ast'][0][0])
            self.assertEqual(m['putter']['ast'][0][1],{'symbol':'LCID'})
            self.assertEqual(m['putter']['ast'][0][2],4)
        self.assertEqual(CONTRACT['hset_setitem']['inputs'],{'itemid':8,'newVal':12})
        self.assertEqual(CONTRACT['hset_setitem']['return_vartype'],24)
    def test_correct_exact_property_get_put_control(self):
        shape=StrictShape([])
        values={'WidthRelTo':4,'HeightRelTo':2,'Width':11339,'Height':5669,'TreatAsChar':1,'AdjustTextbox':1}
        for name,value in values.items():
            setattr(shape,name,value)
            self.assertEqual(getattr(shape,name),value)
        with self.assertRaises(AttributeError):getattr(shape,'AdjustTextBox')
        with self.assertRaises(AttributeError):setattr(shape,'AdjustTextBox',1)
    def test_legacy_permissive_fixture_is_not_installed_accessor_evidence(self):
        old=Shape();old.SetItem('AdjustTextBox',1)
        self.assertEqual(old.AdjustTextBox,1)
        strict=StrictShape([]);strict.SetItem('AdjustTextBox',1)
        with self.assertRaises(AttributeError):getattr(strict,'AdjustTextBox')
    def test_strict_metadata_backed_public_creation_has_eligible_positive(self):
        owned,shape,trace=strict_owned(self)
        result,error=owned.run(CREATE)
        self.assertIsNone(error)
        proof=retain(self,owned,trace,result)
        self.assertTrue(result['ok'],proof)
        self.assertFalse(owned.session._native_custody_held)
        self.assertEqual({n:shape._values[n] for n in MEMBERS},
                         {'WidthRelTo':4,'HeightRelTo':2,'Width':11339,'Height':5669,'TreatAsChar':1,'AdjustTextbox':1})
        self.assertIn('DrawObjCreatorTextBox',str(owned.hwp.executes()))
        self.assertNotIn('AdjustTextBox',shape._values)
    def test_correct_getter_raise_holds_before_execute_and_projection(self):
        owned,shape,trace=strict_owned(self,faults={'AdjustTextbox':RuntimeError('private synthetic read fault')})
        original=rt._native_diagnostic
        def projected(*args):
            self.assertTrue(owned.session._native_custody_held)
            return original(*args)
        with patch.object(rt,'_native_diagnostic',side_effect=projected):result,error=owned.run(CREATE)
        self.assertIsNone(error);retain(self,owned,trace,result)
        facts=assert_read_hold(self,owned,trace,result)
        self.assertIn(['FAULT','injected_read','AdjustTextbox'],trace)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),('container.ShapeObject.get.AdjustTextbox','raised'))
        self.assertNotIn('private synthetic',json.dumps(result))
    def test_ui2_exact_maximum_is_valid_reader_not_ui1(self):
        hwp=ContainersHwp();shape=StrictShape([]);shape.AdjustTextbox=65535
        native=_Native(hwp)
        self.assertEqual(native.call('ShapeObject.get.AdjustTextbox',lambda:shape.AdjustTextbox,'ui2'),65535)
    def test_ui2_rejects_nonexact_or_out_of_representation(self):
        for value in (-1,65536,True,1.0,'1',None):
            with self.subTest(value=value):
                native=_Native(ContainersHwp())
                with self.assertRaises(nn.NativeNumberingReadFailure):
                    native.call('ShapeObject.get.AdjustTextbox',lambda:value,'ui2')
                self.assertTrue(native.failed)
    def test_valid_ui2_does_not_relax_exact_requested_value(self):
        owned,shape,trace=strict_owned(self,faults={'AdjustTextbox':256})
        result,error=owned.run(CREATE)
        self.assertIsNone(error);retain(self,owned,trace,result)
        facts=assert_read_hold(self,owned,trace,result)
        self.assertIn(['FAULT','injected_read','AdjustTextbox'],trace)
        self.assertEqual(facts.get('native_site'),'container.ShapeObject.item_mismatch')
    def test_malformed_ui2_read_latches_without_execute(self):
        owned,shape,trace=strict_owned(self,faults={'AdjustTextbox':True})
        result,error=owned.run(CREATE)
        self.assertIsNone(error);retain(self,owned,trace,result)
        facts=assert_read_hold(self,owned,trace,result)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),('container.ShapeObject.get.AdjustTextbox','true'))


class InstalledAccessorHTTP(unittest.TestCase):
    def test_real_backend_http_typed_healthy_create(self):
        import asyncio
        owned,shape,trace=strict_owned(self)
        status,result=asyncio.run(asgi_post(owned.service,CREATE))
        proof=retain(self,owned,trace,result)
        self.assertEqual(status,200)
        self.assertTrue(result['ok'],proof)
        self.assertFalse(owned.session._native_custody_held)
    def test_real_backend_http_keeps_correct_accessor_read_failure(self):
        import asyncio
        owned,shape,trace=strict_owned(self,faults={'AdjustTextbox':RuntimeError('private synthetic')})
        status,result=asyncio.run(asgi_post(owned.service,CREATE))
        self.assertEqual(status,200);retain(self,owned,trace,result)
        facts=assert_read_hold(self,owned,trace,result)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),('container.ShapeObject.get.AdjustTextbox','raised'))
