"""Public open diagnostics, not a reproduction of the unretained native fault.

Real upload/service/interactive record/runtime manager/router/HTTP serialization;
only readiness admission and native start are modeled under an external fence.
"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from app.config import Settings
from app.interactive_session_manager import InteractiveSessionManager
from app.local_cli_service import LocalCliService
from app.local_cli_router import build_local_cli_router
from app import local_cli_runtime as runtime

SECRET = 'C:\\private\\token=hidden.docx private-document-body'


class OpenFixture:
    def __init__(self, root):
        self.settings = Settings(spool_root=root/'spool', _env_file=None)
        self.interactive = InteractiveSessionManager(self.settings)
        self.service = LocalCliService(settings=self.settings, interactive_sessions=self.interactive)
        self.service.runtime_manager = runtime.LocalCliRuntimeManager()
        # Native admission itself is not qualified by a portable HTTP test.
        self.service._require_ready_runtime = lambda *args: None
        self.app = FastAPI()
        self.app.include_router(build_local_cli_router(settings=self.settings, interactive_sessions=self.interactive, service=self.service))

    def post(self):
        # Exercise real multipart/route/exception JSON serialization without
        # adding the optional TestClient/httpx2 dependency to portable CI.
        import asyncio
        from types import SimpleNamespace
        body=(b'--open-boundary\r\nContent-Disposition: form-data; name="file"; filename="fixture.hwpx"\r\n'
              b'Content-Type: application/octet-stream\r\n\r\ncontract-only upload\r\n--open-boundary--\r\n')
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http',
               'path':'/local-cli/open','raw_path':b'/local-cli/open','root_path':'','query_string':b'',
               'headers':[(b'content-type',b'multipart/form-data; boundary=open-boundary'),(b'content-length',str(len(body)).encode())],
               'server':('127.0.0.1',80),'client':('127.0.0.1',1)}
        sent=[]
        async def receive():return {'type':'http.request','body':body,'more_body':False}
        async def send(message):sent.append(message)
        asyncio.run(self.app(scope,receive,send))
        text=b''.join(v.get('body',b'') for v in sent).decode()
        return SimpleNamespace(status_code=sent[0]['status'],text=text,json=lambda:json.loads(text))

    def dispose(self):
        # Tests with mocked start never acquired an SDK instance or started STA.
        self.service.runtime_manager._sessions.clear()
        for handler in list(self.interactive.logger.handlers):
            if getattr(handler, 'baseFilename', '').startswith(str(self.settings.spool_root)):
                handler.close();self.interactive.logger.removeHandler(handler)


class PublicOpenDiagnostic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.fixture = OpenFixture(Path(self.tmp.name));self.addCleanup(self.fixture.dispose)

    def failure(self, response, stage, category):
        self.assertEqual(response.status_code, 500, response.text)
        value = response.json()['detail']
        self.assertIs(type(value), dict, 'unexpected open failure currently loses its discriminating stage/type')
        self.assertEqual(value['open_failure'], {'code':'open_failed','stage':stage,'exception_class':category})
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn('private-document-body', response.text)
        return value

    def test_layout_error_has_fixed_stage_and_type_without_native_entry(self):
        with patch('app.local_cli_service.ensure_session_layout', side_effect=PermissionError(SECRET)), patch.object(runtime.LocalCliLiveSession, 'start') as start:
            self.failure(self.fixture.post(), 'session_layout', 'permission_error')
        start.assert_not_called()

    def test_manager_error_keeps_distinct_stage_and_safe_type(self):
        with patch.object(runtime.LocalCliLiveSession, '__init__', side_effect=TypeError(SECRET)):
            self.failure(self.fixture.post(), 'runtime_session_create', 'type_error')

    def test_healthy_open_still_binds_exact_managed_copy(self):
        with patch.object(runtime.LocalCliLiveSession, 'start', return_value={'ok':True,'location':{}}) as start:
            response=self.fixture.post()
        self.assertEqual(response.status_code, 200, response.text)
        value=response.json();self.assertTrue(value['ok']);self.assertNotIn('open_failure',value)
        binding=self.fixture.service._read_binding(session_id=value['session_id'])
        self.assertEqual(Path(binding['working_copy_path']).read_bytes(),b'contract-only upload')
        self.assertEqual(binding['session_id'],value['session_id']);start.assert_called_once()


    def test_interactive_record_error_is_safe_and_distinct(self):
        with patch.object(self.fixture.interactive, 'open_session', side_effect=PermissionError(SECRET)):
            response=self.fixture.post()
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['detail']['open_failure'],{'code':'open_failed','stage':'interactive_record','exception_class':'permission_error'})
        self.assertNotIn(SECRET,response.text)

    def test_runtime_unknown_keeps_first_label_and_makes_no_sdk_follow_on(self):
        import sys, threading, types
        from unittest.mock import Mock
        sessions=self.fixture.service.runtime_manager
        constructor=Mock(side_effect=TypeError(SECRET));close=Mock();opened=Mock();snapshot=Mock()
        pythoncom=types.SimpleNamespace(CoInitialize=Mock(),CoUninitialize=Mock())
        fake=types.ModuleType('pyhwpx');fake.Hwp=object
        waiting=threading.Event();original_finalize=runtime.LocalCliLiveSession._finalize
        def finalize(session,**kw):
            waiting.set();return original_finalize(session,**kw)
        with patch.dict(sys.modules,{'pyhwpx':fake,'pythoncom':pythoncom}), patch.object(runtime,'current_windows_session_snapshot',return_value={'interactive':True}), patch.object(runtime,'_native_close_processes',return_value=None), patch.object(runtime,'create_hwp_instance_with_recovery',constructor), patch.object(runtime,'close_hwp_instance',close), patch.object(runtime,'open_document_with_recovery',opened), patch.object(runtime,'collect_hwp_snapshot',snapshot), patch.object(runtime,'update_runtime_status'), patch.object(runtime.LocalCliLiveSession,'_finalize',finalize):
            try:
                self.failure(self.fixture.post(),'runtime_constructor','type_error')
                self.assertTrue(waiting.wait(2));self.assertEqual(len(sessions._sessions),1)
                session=next(iter(sessions._sessions.values()))
                self.assertTrue(session._native_custody_held and session._quarantined)
                self.assertTrue(session._thread.is_alive());self.assertFalse(session._allow_native_cleanup)
                constructor.assert_called_once();close.assert_not_called();opened.assert_not_called();snapshot.assert_not_called()
                pythoncom.CoUninitialize.assert_not_called()
            finally:
                # Test-only release of a model with NO native object. Not native
                # closure authority; do not issue a diagnostic getter or close.
                for session in list(sessions._sessions.values()):
                    waiting.wait(2);session._allow_native_cleanup=True
                    session._commands.put(runtime._LiveCommand('test-release','__close__',None,runtime.Future()))
                    session._thread.join(3);self.assertFalse(session._thread.is_alive())
                close.assert_not_called();pythoncom.CoUninitialize.assert_not_called()


class LabelPrivacy(unittest.TestCase):
    def test_first_capsule_survives_wrapping_without_exception_formatting(self):
        from app.open_failure import record_failure,failure_from_exception
        class PrivateError(RuntimeError):
            def __str__(self):raise AssertionError('do not format exception')
            def __repr__(self):raise AssertionError('do not format exception')
            def __getattribute__(self,name):raise AssertionError('no custom exception getter')
        error=PrivateError(SECRET);record_failure(error,'runtime_constructor')
        wrapped=RuntimeError();record_failure(wrapped,'runtime_open',origin=error)
        record_failure(wrapped,'open_dispatch')
        self.assertEqual(failure_from_exception(wrapped),{'code':'open_failed','stage':'runtime_constructor','exception_class':'runtime_error'})

        # A later Python reporting/finally error cannot replace an already
        # captured first stage. No raw context/traceback reaches the serializer.
        from app.local_cli_service import as_http_error
        try:
            raise wrapped
        except RuntimeError:
            try:
                raise ValueError(SECRET)
            except ValueError as later:
                reply=as_http_error(later,open_failure_stage='open_dispatch')
        self.assertEqual(reply.detail['open_failure'],failure_from_exception(wrapped))

    def test_public_projection_refuses_foreign_labels_and_extra_data(self):
        from app.open_failure import public_failure
        good={'code':'open_failed','stage':'session_layout','exception_class':'os_error'}
        self.assertEqual(public_failure(good),good)
        for bad in ({**good,'raw':SECRET},{**good,'stage':SECRET},{**good,'exception_class':'PrivateDocumentError'},
                    {**good,'exception_class':[]},{**good,'code':True},None):
            self.assertIsNone(public_failure(bad))

    def test_unrelated_command_and_expected_refusal_contracts_are_unchanged(self):
        from app.local_cli_service import as_http_error,LocalCliServiceError
        self.assertEqual(as_http_error(RuntimeError(SECRET)).detail,'Local CLI request failed.')
        expected=LocalCliServiceError('Empty upload is not allowed.',status_code=400)
        self.assertEqual(as_http_error(expected,open_failure_stage='open_dispatch').detail,expected.message)
        timeout=runtime.LocalCliRuntimeTimeoutError('reconcile command',command_id='test:start',command_state='timed_out_pending_reconciliation')
        pending=LocalCliServiceError('command_id=test:start; run command-reconcile',status_code=504);pending.__cause__=timeout
        self.assertEqual(as_http_error(pending,open_failure_stage='open_dispatch').detail,pending.message)


if __name__=='__main__':unittest.main()
