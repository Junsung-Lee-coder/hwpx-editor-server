"""Official MCP client -> real MCP/FastAPI/multipart/service/manager open.

SDK-free diagnostic composition only; no modeled fault is historical cause or
native output. The only mocked native boundary here is LocalCliLiveSession.start.
"""
import json
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx2
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from hwpx_mcp.server import Settings, build_app, PROTOCOL_VERSION
from tests.test_open_failure_diagnostic import OpenFixture, SECRET
from app import local_cli_runtime as runtime


class ComposedOpen(unittest.IsolatedAsyncioTestCase):
    def serve(self,app,port=0):
        sock=socket.socket();sock.bind(('127.0.0.1',port));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='warning',timeout_graceful_shutdown=3))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]});thread.start()
        def close():
            server.should_exit=True;thread.join(8);sock.close()
            self.assertFalse(thread.is_alive(),'test-owned HTTP server leaked')
        self.addCleanup(close)
        until=time.monotonic()+10
        while not server.started and thread.is_alive() and time.monotonic()<until:time.sleep(.01)
        self.assertTrue(server.started)
        return port

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.fixture=OpenFixture(self.root);self.addCleanup(self.fixture.dispose)
        (self.root/'input').mkdir();self.source=self.root/'input/fixture.hwpx';self.source.write_bytes(b'contract-only upload')
        self.backend_port=self.serve(self.fixture.app)
        self.token=secrets.token_urlsafe(32)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));mcp_port=sock.getsockname()[1]
        settings=Settings(token=self.token,source_root=self.root/'input',artifact_root=self.root/'artifacts',backend=f'http://127.0.0.1:{self.backend_port}',port=mcp_port)
        self.port=self.serve(build_app(settings),mcp_port);self.open_calls=[]

    async def call(self):
        async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+self.token},trust_env=False,timeout=20) as http:
            async with Client(streamable_http_client(f'http://127.0.0.1:{self.port}/mcp',http_client=http),mode=PROTOCOL_VERSION) as client:
                value=await client.call_tool('hwpx_open',{'request':{'source_path':str(self.source)}})
                self.open_calls.append('hwpx_open')
                return value

    async def test_actual_serializer_preserves_layout_stage_and_fixed_type(self):
        with patch('app.local_cli_service.ensure_session_layout',side_effect=PermissionError(SECRET)),patch.object(runtime.LocalCliLiveSession,'start') as start:
            response=await self.call()
        value=response.structured_content
        self.assertTrue(response.is_error);self.assertFalse(value['ok']);self.assertIsNone(value['session_id'])
        self.assertEqual(value['error']['code'],'BACKEND_REJECTED')
        self.assertEqual(value['error']['details']['open_failure'],{'code':'open_failed','stage':'session_layout','exception_class':'permission_error'})
        self.assertEqual(value['error']['details']['http_status'],500)
        self.assertNotIn('private-document-body',json.dumps(value));self.assertNotIn(SECRET,json.dumps(value))
        self.assertEqual(self.open_calls,['hwpx_open']);start.assert_not_called()

    async def test_healthy_open_still_reaches_real_service_binding_and_consumer(self):
        with patch.object(runtime.LocalCliLiveSession,'start',return_value={'ok':True,'location':{}}) as start:
            response=await self.call()
        value=response.structured_content;self.assertFalse(response.is_error,value);self.assertTrue(value['ok'])
        sid=value['session_id'];self.assertEqual(len(sid),32)
        binding=self.fixture.service._read_binding(session_id=sid)
        self.assertEqual(Path(binding['working_copy_path']).read_bytes(),self.source.read_bytes())
        self.assertNotIn('open_failure',json.dumps(value));start.assert_called_once()

    async def test_manager_constructor_error_is_not_relabelled_as_sdk_constructor(self):
        with patch.object(runtime.LocalCliLiveSession,'__init__',side_effect=TypeError(SECRET)):
            value=(await self.call()).structured_content
        self.assertEqual(value['error']['details']['open_failure'],{'code':'open_failed','stage':'runtime_session_create','exception_class':'type_error'})
        self.assertNotIn('private-document-body',json.dumps(value))

    async def test_malicious_foreign_backend_detail_is_not_public_telemetry(self):
        # Privacy-only adversarial response, NOT the cause of the native500.
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from hwpx_mcp.server import Facade,BackendFailure
        app=FastAPI()
        @app.post('/local-cli/open')
        def bad():
            return JSONResponse(status_code=500,content={'detail':{'message':SECRET,'raw':SECRET,
                'open_failure':{'code':'open_failed','stage':SECRET,'exception_class':'PrivateDocumentError'}}})
        transport=httpx2.ASGITransport(app=app)
        settings=Settings(token=self.token,source_root=self.root/'input',artifact_root=self.root/'artifacts')
        async with httpx2.AsyncClient(transport=transport) as http:
            with self.assertRaises(BackendFailure) as failed:
                await Facade(settings).request(http,'POST','/local-cli/open')
        self.assertNotIn('private-document-body',json.dumps(failed.exception.details))
        self.assertNotIn('PrivateDocumentError',json.dumps(failed.exception.details))
        self.assertNotIn('open_failure',failed.exception.details.get('backend',{}).get('detail',{}))


if __name__=='__main__':unittest.main()
