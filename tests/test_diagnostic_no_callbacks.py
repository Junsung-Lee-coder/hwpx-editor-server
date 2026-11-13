"""D1 finite diagnostic boundary: no native SDK, no callback/first-error loss."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import sys, tempfile, threading, types, unittest
from unittest.mock import Mock, patch
from app import local_cli_runtime as rt
from app import open_failure as diagnostic
from app.local_cli_service import as_http_error


class CallbackDict(dict):
    def __init__(self, events):super().__init__();self.events=events
    def get(self,*a):self.events.append('get');raise RuntimeError('forbidden callback')
    def __setitem__(self,*a):self.events.append('setitem');raise RuntimeError('forbidden callback')
    def items(self):self.events.append('items');raise RuntimeError('forbidden callback')
    def __iter__(self):self.events.append('iter');raise RuntimeError('forbidden callback')
    def __len__(self):self.events.append('len');raise RuntimeError('forbidden callback')
    def __del__(self):self.events.append('del')


class ForeignKey:
    def __init__(self,key,events):self.key=key;self.events=events
    def __hash__(self):self.events.append('hash');return hash(self.key)
    def __eq__(self,other):self.events.append('eq');return other==self.key


@contextmanager
def model(constructor):
    pythoncom=types.SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())
    fake=types.ModuleType('pyhwpx');fake.Hwp=object
    waiting=threading.Event();original=rt.LocalCliLiveSession._finalize
    def finalize(session,**kwargs):waiting.set();return original(session,**kwargs)
    close,opened,snapshot=Mock(),Mock(),Mock()
    with ExitStack() as stack:
        for p in (patch.dict(sys.modules,{'pyhwpx':fake,'pythoncom':pythoncom}),
                  patch.object(rt,'current_windows_session_snapshot',return_value={'interactive':True}),
                  patch.object(rt,'_native_close_processes',return_value=None),
                  patch.object(rt,'create_hwp_instance_with_recovery',constructor),
                  patch.object(rt,'close_hwp_instance',close),patch.object(rt,'open_document_with_recovery',opened),
                  patch.object(rt,'collect_hwp_snapshot',snapshot),patch.object(rt,'update_runtime_status'),
                  patch.object(rt.LocalCliLiveSession,'_finalize',finalize)):
            stack.enter_context(p)
        yield types.SimpleNamespace(waiting=waiting,pythoncom=pythoncom,close=close,opened=opened,snapshot=snapshot)


class CapsuleSafety(unittest.TestCase):
    def test_dict_subclass_is_inspected_and_stored_without_any_callback_or_purge(self):
        events=[];error=TypeError('private');state=CallbackDict(events)
        sentinel=object();dict.__setitem__(state,'original',sentinel);error.__dict__=state
        diagnostic.record_failure(error,'runtime_constructor')
        self.assertEqual(events,[])
        self.assertEqual(diagnostic.failure_from_exception(error),{'code':'open_failed','stage':'runtime_constructor','exception_class':'type_error'})
        self.assertIs(error.__dict__['original'],sentinel)
        self.assertEqual(events,[],'including no eager destruction of the prior mapping')

    def test_foreign_exception_keys_are_not_hashed_compared_or_purged_on_write(self):
        events=[];error=RuntimeError('private');key=ForeignKey(diagnostic._KEY,events)
        error.__dict__[key]=object();original=error.__dict__;events.clear()
        diagnostic.record_failure(error,'open_dispatch')
        self.assertEqual(events,[]);self.assertIs(error.__dict__,original)
        self.assertIsNone(diagnostic.failure_from_exception(error))
        self.assertEqual(as_http_error(error,open_failure_stage='open_dispatch').status_code,500)
        self.assertEqual(events,[])

    def test_foreign_public_keys_are_rejected_before_hash_equality_or_lookup(self):
        for label in ('stage','code','exception_class'):
            events=[];value={'stage':'session_layout','code':'open_failed','exception_class':'os_error'}
            replacement=value.pop(label);value[ForeignKey(label,events)]=replacement;events.clear()
            self.assertIsNone(diagnostic.public_failure(value));self.assertEqual(events,[])

    def test_foreign_input_and_stage_never_run_attribute_or_hash_callbacks(self):
        events=[]
        class Foreign:
            def __getattribute__(self,name):events.append(name);raise RuntimeError('getter')
        self.assertIsNone(diagnostic.failure_from_exception(Foreign()));self.assertEqual(events,[])
        error=ValueError('private');stage=ForeignKey('session_layout',events)
        diagnostic.record_failure(error,stage);self.assertEqual(events,[])
        self.assertIsNone(diagnostic.failure_from_exception(error))

    def test_first_valid_capsule_and_all_original_attributes_survive(self):
        error=PermissionError('private');error.__dict__.update({f'attribute_{i}':object() for i in range(80)})
        original=dict(error.__dict__);diagnostic.record_failure(error,'session_layout')
        for key,value in original.items():self.assertIs(error.__dict__[key],value)
        diagnostic.record_failure(error,'working_copy')
        self.assertEqual(diagnostic.failure_from_exception(error)['stage'],'session_layout')
        # A later foreign collision cannot erase an already validated first capsule.
        events=[];error.__dict__[ForeignKey('unrelated',events)]=object();events.clear()
        self.assertEqual(diagnostic.failure_from_exception(error)['stage'],'session_layout');self.assertEqual(events,[])

    def test_capsule_value_does_not_dispatch_field_getters(self):
        events=[];error=RuntimeError('private')
        class Foreign:
            def __getattribute__(self,name):events.append(name);raise RuntimeError('getter')
        error.__dict__[diagnostic._KEY]=Foreign()
        diagnostic.record_failure(error,'working_copy')
        self.assertEqual(diagnostic.failure_from_exception(error)['stage'],'working_copy');self.assertEqual(events,[])


class ReporterSettlement(unittest.TestCase):
    def test_http_reporter_failure_preserves_original_status_without_leaking(self):
        original=PermissionError('private-path-token')
        with patch('app.local_cli_service.record_failure',side_effect=MemoryError('reporter-private')):
            response=as_http_error(original,open_failure_stage='open_dispatch')
        self.assertEqual(response.status_code,500);self.assertEqual(response.detail,'Local CLI request failed.')

    def test_actual_run_future_settles_original_cause_if_reporter_itself_fails(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);session=rt.LocalCliLiveSession(session_id='model',session_root=root,working_copy_path=root/'fixture.hwpx',source_filename='fixture.hwpx')
            original=TypeError('original-private');constructor=Mock(side_effect=original)
            with model(constructor) as m,patch.object(rt,'record_failure',side_effect=MemoryError('reporter-private')):
                try:
                    session._thread.start();self.assertTrue(m.waiting.wait(3))
                    self.assertTrue(session._native_custody_held and session._quarantined and session._thread.is_alive())
                    self.assertTrue(session._start_future.done(),'reporting must not strand the startup Future')
                    delivered=session._start_future.exception();self.assertIsInstance(delivered,rt.LocalCliNativeCustodyHeldError)
                    self.assertIs(delivered.__cause__,original)
                    m.close.assert_not_called();m.opened.assert_not_called();m.snapshot.assert_not_called();m.pythoncom.CoUninitialize.assert_not_called()
                finally:
                    session._allow_native_cleanup=True
                    session._commands.put(rt._LiveCommand('test-model-release','__close__',None,rt.Future()))
                    session._thread.join(3);self.assertFalse(session._thread.is_alive())
                    m.close.assert_not_called();m.pythoncom.CoUninitialize.assert_not_called()

if __name__=='__main__':unittest.main()
