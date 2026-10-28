"""Real constructor/manager/STA/finalizer, locked wrapper bodies, fake OS+COM only."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import sys
import tempfile
import threading
import unittest
from app import local_cli_runtime as rt
from app import worker
from app.local_cli_service import LocalCliService


class NativeCloseTests(unittest.TestCase):
    def owned(self, *, startup_error=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        wc = root / 'working.hwpx'
        wc.write_bytes(b'fixture')
        events = []
        state = {'modified': False, 'exit': True, 'quit_result': None, 'quit_error': None}
        document = NS(DocumentID=23, FullName=str(wc))
        docs = NS(Count=1, Active_XHwpDocument=document)
        windows = NS(Count=1, Active_XHwpWindow=NS(WindowHandle=29))
        class SDK:
            XHwpDocuments = docs
            XHwpWindows = windows
            Path = str(wc)
            @property
            def IsModified(self):
                events.append(('modified', threading.get_ident()))
                return state['modified']
            def Quit(self):
                events.append(('quit', threading.get_ident()))
                if state['quit_error']:
                    raise state['quit_error']
                return state['quit_result']
        sdk = SDK()
        com = NS(CoInitialize=lambda: events.append(('initialize', threading.get_ident())),
                 CoUninitialize=lambda: events.append(('uninitialize', threading.get_ident())))
        fixture = Path(__file__).parent / 'fixtures' / 'pyhwpx_172_lifecycle.py'
        def construct_context(index):
            events.append(('construct', threading.get_ident()))
            return object()
        com.CreateBindCtx = construct_context
        com.GetRunningObjectTable = lambda: NS(EnumRunning=lambda: [])
        namespace = {'pythoncom': com, 'fonts': {},
                     'win32': NS(gencache=NS(EnsureDispatch=lambda name: sdk))}
        exec(compile(fixture.read_text(), str(fixture), 'exec'), namespace)
        Wrapper = namespace['Hwp']
        class Process:
            def verify(self): events.append(('verify_process', threading.get_ident()))
            def wait(self):
                events.append(('wait_exit', threading.get_ident()))
                if not state['exit']: raise rt.LocalCliRuntimeError('exit unknown')
            def close(self): events.append(('handle_close', threading.get_ident()))
        def open_document(**kw):
            if startup_error is not None:
                raise startup_error
            return kw['hwp']
        for name,value in (
            ('_native_close_processes', lambda: frozenset()),
            ('_bind_native_close_process', lambda hwnd, before: Process()),
            ('open_document_with_recovery', open_document),
            ('start_runtime_watchdog', lambda *a, **k: (None,None)),
            ('update_runtime_status', lambda *a, **k: None),
            ('current_windows_session_snapshot', lambda: {'interactive':True}),
            ('collect_hwp_snapshot', lambda h: {}),
            ('snapshot_live_location', lambda **k: {}),
        ):
            stack.enter_context(patch.object(rt,name,value,create=True))
        stack.enter_context(patch.object(worker,'configure_hwp_automation',lambda h: {}))
        stack.enter_context(patch.object(worker,'update_runtime_status',lambda *a, **k: None))
        stack.enter_context(patch.dict(sys.modules,{'pythoncom':com,'pyhwpx':NS(Hwp=Wrapper)}))
        manager=rt.LocalCliRuntimeManager()
        if startup_error is None:
            manager.open_session(session_id='close-case',session_root=root,working_copy_path=wc,source_filename='fixture.hwpx')
            session=manager._sessions['close-case']
        else:
            session=rt.LocalCliLiveSession(session_id='close-case',session_root=root,working_copy_path=wc,source_filename='fixture.hwpx')
            manager._sessions['close-case']=session
            manager._session_roots['close-case']=root
        def release():
            if session._thread.is_alive():
                session._allow_native_cleanup=True
                with patch.object(rt.LocalCliLiveSession,'_finalize',lambda *a, **k: None):
                    session._commands.put(rt._LiveCommand('test-release','__close__',None,rt.Future()))
                    session._thread.join(2)
            self.assertFalse(session._thread.is_alive(), 'fake STA leaked')
        self.addCleanup(release)
        if startup_error is not None:
            with self.assertRaises(rt.LocalCliRuntimeError): session.start(timeout=2)
        return manager,session,events,state,docs,windows,document

    def test_real_process_identity_helper_rejects_preexisting_and_changed_generation(self):
        from app import readiness
        calls=[]
        handle=NS(Close=lambda:calls.append('close_handle'))
        generation=['original']
        waits=[258]
        fake_api=NS(OpenProcess=lambda *a:handle)
        fake_event=NS(WaitForSingleObject=lambda h,ms: waits[0] if ms==0 else 0)
        fake_process=NS(GetWindowThreadProcessId=lambda h:(1,900001))
        fake_ts=NS(ProcessIdToSessionId=lambda pid:7)
        with patch.dict(sys.modules,{'win32api':fake_api,'win32event':fake_event,'win32process':fake_process,'win32ts':fake_ts}), \
                patch.object(rt,'collect_process_snapshot',return_value={'alive':True,'image_name':'Hwp.exe'}), \
                patch.object(readiness,'_process_start_identity',side_effect=lambda pid:generation[0]):
            with self.assertRaises(rt.LocalCliRuntimeError): rt._NativeCloseProcess(900002,frozenset({900001}))
            self.assertEqual(calls,[])
            process=rt._NativeCloseProcess(900002,frozenset())
            process.verify()
            process.wait()
            generation[0]='reused'
            with self.assertRaises(rt.LocalCliRuntimeError): process.verify()
            generation[0]='original'
            waits[0]=0
            with self.assertRaises(rt.LocalCliRuntimeError): process.verify()
            process.close()
            self.assertEqual(calls,['close_handle'])

    def test_baseline_parser_does_not_admit_failed_or_malformed_snapshot(self):
        result=NS(stdout='"Hwp.exe","900001","Console","7","123 K"\n"other.exe","900003","Console","7","123 K"\n')
        with patch.object(rt.os.sys,'platform','win32'),patch.object(rt.subprocess,'run',return_value=result) as run:
            self.assertEqual(rt._native_close_processes(),frozenset({900001}))
            self.assertTrue(run.call_args.kwargs['check'])
            result.stdout='unreadable'
            with self.assertRaises(rt.LocalCliRuntimeError): rt._native_close_processes()

    def test_document_clear_void_return_is_not_native_exit_proof(self):
        calls=[]
        wrapper=NS(clear=lambda **kw:calls.append(kw))
        self.assertIs(rt.discard_live_document(wrapper),True)
        self.assertEqual(calls,[{'option':1}])
        for value in (True,False,0):
            with self.subTest(value=value):
                with self.assertRaises(rt.LocalCliRuntimeError):
                    rt.discard_live_document(NS(clear=lambda **kw:value))

    def test_manager_normal_close_void_quit_no_clear_and_both_com_balances(self):
        manager,session,events,state,*_ = self.owned()
        owner=session._thread.ident
        manager.close_session('close-case',timeout=2)
        self.assertTrue(session._closed.is_set())
        self.assertFalse(session._thread.is_alive())
        self.assertNotIn('close-case',manager._sessions)
        names=[e[0] for e in events]
        self.assertEqual(names.count('quit'),1)
        self.assertEqual(names.count('uninitialize'),2)
        self.assertEqual(names.count('construct'),1)
        self.assertNotIn('clear',names)
        self.assertLess(names.index('quit'),names.index('wait_exit'))
        self.assertLess(names.index('wait_exit'),names.index('uninitialize'))
        self.assertTrue(all(e[1]==owner for e in events if e[0] in ('quit','uninitialize','wait_exit')))
        self.assertEqual(session.command_status()['state'],'succeeded')
        private=session._command_record(session._commands_by_id[session._last_command_id],include_private=True)
        self.assertEqual(private['native_close_diagnostic'],{
            'quit_entered':True,'quit_return_arrived':True,'quit_void_return':True,
            'process_exit_confirmed':True,'wrapper_com_return_arrived':True,'runtime_com_return_arrived':True})
        self.assertNotIn('native_close_diagnostic',session.command_status())
        session.close(timeout=.1)
        self.assertEqual([e[0] for e in events].count('quit'),1)

    def test_unknowns_preserve_original_sta_registry_and_no_later_sdk(self):
        for fault in ('modified','doc_count','window_count','doc_identity','path','quit_false','quit_error','exit'):
            with self.subTest(fault=fault):
                manager,session,events,state,docs,windows,document=self.owned()
                if fault=='modified': state['modified']=None
                elif fault=='doc_count': docs.Count=2
                elif fault=='window_count': windows.Count=2
                elif fault=='doc_identity': document.DocumentID=99
                elif fault=='path': document.FullName='other.hwpx'
                elif fault=='quit_false': state['quit_result']=False
                elif fault=='quit_error': state['quit_error']=ValueError('PRIVATE')
                elif fault=='exit': state['exit']=False
                with self.assertRaises(rt.LocalCliRuntimeError): manager.close_session('close-case',timeout=2)
                self.assertTrue(session._thread.is_alive())
                self.assertFalse(session._closed.is_set())
                self.assertTrue(session._native_custody_held)
                self.assertEqual(manager.reap_settled_sessions(),0)
                names=[e[0] for e in events]
                self.assertEqual(names.count('quit'),1 if fault.startswith('quit') or fault=='exit' else 0)
                self.assertNotIn('uninitialize',names)
                prior=list(events)
                with self.assertRaises(rt.LocalCliRuntimeError): manager.close_session('close-case',timeout=.1)
                self.assertEqual(events,prior)

    def test_completed_close_is_not_replayed_or_relabelled_by_reporting_failure(self):
        for site in ('journal','status','cancel'):
            with self.subTest(site=site):
                manager,session,events,state,*_=self.owned()
                persist=session._persist_command
                def report(command):
                    if command.state=='succeeded': raise OSError('PRIVATE')
                    return persist(command)
                if site=='journal': target=patch.object(session,'_persist_command',report)
                elif site=='status': target=patch.object(rt,'update_runtime_status',side_effect=OSError('PRIVATE'))
                else: target=patch.object(session,'_cancel_pending_commands',side_effect=OSError('PRIVATE'))
                with target:
                    with self.assertRaises(rt.LocalCliRuntimeError): manager.close_session('close-case',timeout=2)
                self.assertTrue(session._closed.is_set())
                session._thread.join(1)
                self.assertFalse(session._thread.is_alive())
                self.assertEqual(session.command_status()['state'],'succeeded')
                self.assertIs(session.command_status()['semantic_ok'],True)
                manager.close_session('close-case',timeout=1)
                self.assertEqual([e[0] for e in events].count('quit'),1)

    def test_startup_unknown_retains_sta_without_finalizer_quit(self):
        manager,session,events,state,*_=self.owned(startup_error=ValueError('PRIVATE'))
        self.assertTrue(session._thread.is_alive())
        self.assertFalse(session._closed.is_set())
        self.assertTrue(session._native_custody_held)
        before=list(events)
        self.assertNotIn('quit',[e[0] for e in events])
        self.assertNotIn('uninitialize',[e[0] for e in events])
        with self.assertRaises(rt.LocalCliRuntimeError): manager.close_session('close-case',timeout=.1)
        self.assertEqual(events,before)

    def test_public_reporting_error_keeps_native_completed_status_honest(self):
        from app.local_cli_service_support import LocalCliServiceError
        manager,session,events,state,*_=self.owned()
        binding={'session_id':'close-case'}
        saved=[]
        service=object.__new__(LocalCliService)
        service.runtime_manager=manager
        service._read_binding=lambda **k:binding
        service._binding_session_id=lambda binding:'close-case'
        service._binding_has_pending_reconciliation=lambda binding:False
        service._save_binding=lambda binding:saved.append(dict(binding)) or binding
        with patch.object(session,'_cancel_pending_commands',side_effect=OSError('PRIVATE')):
            with self.assertRaises(LocalCliServiceError) as caught: service.close()
        self.assertIn('Native close completed',caught.exception.message)
        self.assertEqual(saved[-1]['document_session_state'],'closed_cleanup_pending')
        self.assertIs(saved[-1]['live_session_bound'],False)
        self.assertTrue(session._closed.is_set())
        self.assertEqual(session.command_status()['state'],'succeeded')

    def test_failed_com_completion_is_held_and_destructor_is_not_replayed(self):
        manager,session,events,state,*_=self.owned()
        wrapper=session._native_close_owner['wrapper']
        namespace=type(wrapper).__del__.__globals__
        com=namespace['pythoncom']
        def fail():
            events.append(('uninitialize_failed',threading.get_ident()))
            raise OSError('PRIVATE')
        com.CoUninitialize=fail
        with self.assertRaises(rt.LocalCliRuntimeError): manager.close_session('close-case',timeout=2)
        self.assertFalse(session._closed.is_set())
        self.assertTrue(session._thread.is_alive())
        self.assertEqual([e[0] for e in events].count('quit'),1)
        self.assertEqual([e[0] for e in events].count('uninitialize_failed'),1)
        # The exact locked destructor is now disarmed, including delayed GC.
        type(wrapper).__del__(wrapper)
        self.assertEqual([e[0] for e in events].count('uninitialize_failed'),1)

    def test_cli_main_transport_service_manager_close_positive_and_held(self):
        import contextlib
        import io
        from local_cli_v1 import main as cli
        from app.local_cli_service_support import LocalCliServiceError
        for held in (False,True):
            with self.subTest(held=held):
                manager,session,events,state,*_=self.owned()
                if held: state['modified']=None
                service=object.__new__(LocalCliService)
                service.runtime_manager=manager
                service._read_binding=lambda **k:{'session_id':'close-case'}
                service._binding_session_id=lambda binding:'close-case'
                service._binding_has_pending_reconciliation=lambda binding:False
                service._record_session_close=lambda **k:None
                service._clear_binding=lambda **k:None
                service._mark_session_closed=lambda *a:None
                transport=[]
                cleared=[]
                def post(base,path,payload):
                    transport.append(('POST',path,payload))
                    try: return service.close()
                    except LocalCliServiceError: raise cli.ApiError('close held')
                def get(base,path):
                    transport.append(('GET',path))
                    return {'live_session_bound':not session._closed.is_set()}
                with patch.object(cli,'load_state',return_value={'state_generation':1,'source_filename':'fixture.hwpx'}), \
                        patch.object(cli,'_state_session_id',return_value='close-case'), \
                        patch.object(cli,'_read_candidate_identity',return_value={}), \
                        patch.object(cli,'clear_session_binding',side_effect=lambda **k:cleared.append(k)), \
                        patch.object(cli,'post_json',post),patch.object(cli,'get_json',get), \
                        contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    result=cli.main(['--base-url','http://127.0.0.1:9000','close','--json'])
                self.assertEqual(result,1 if held else 0)
                self.assertEqual(len(cleared),0 if held else 1)
                self.assertEqual([e[0] for e in events].count('quit'),0 if held else 1)
                self.assertEqual(len(transport),1 if held else 2)

    def test_public_service_close_only_cleans_managed_files_after_native_complete(self):
        manager,session,events,state,*_=self.owned()
        service=object.__new__(LocalCliService)
        service.runtime_manager=manager
        service._read_binding=lambda **k: {'session_id':'close-case'}
        service._binding_session_id=lambda b:'close-case'
        service._binding_has_pending_reconciliation=lambda b:False
        service._record_session_close=lambda **k:events.append(('record_closed',))
        service._clear_binding=lambda **k:events.append(('clear_binding',))
        service._mark_session_closed=lambda *a:events.append(('mark_closed',))
        self.assertTrue(service.close()['ok'])
        names=[e[0] for e in events]
        self.assertLess(names.index('uninitialize'),names.index('clear_binding'))


if __name__=='__main__': unittest.main()
