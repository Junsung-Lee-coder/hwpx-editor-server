"""Socket-level tests using the pinned official SDK and an isolated fake REST backend."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
from jsonschema import Draft202012Validator
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from hwpx_mcp.server import build_app

VERSION = '2026-07-28'
SID = 'a' * 32
OTHER = 'b' * 32
META = {'io.modelcontextprotocol/protocolVersion': VERSION,
        'io.modelcontextprotocol/clientCapabilities': {}}


class Backend(BaseHTTPRequestHandler):
    calls = []
    mode = 'normal'

    def log_message(self, format, *args):
        pass

    def reply(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except ConnectionError:
            pass

    def do_GET(self):
        type(self).calls.append(('GET', self.path, None))
        if self.path == '/health':
            if type(self).mode == 'delay':
                time.sleep(2)
            if type(self).mode == 'failure':
                return self.reply({'ok': False, 'error': {'code': 'NATIVE_BLOCKED', 'message': 'blocked'}}, 409)
            return self.reply({'ok': True, 'queue_depth': 0, 'running_jobs': 0})
        if self.path.startswith('/interactive/session/status?session_id='):
            sid = self.path.split('=')[-1]
            if sid != SID:
                return self.reply({'detail': 'session not found'}, 404)
            return self.reply({'ok': True, 'session': {'session_id': SID, 'state': 'idle',
                               'metadata': {'local_cli_v1': {'opened_via': 'local_cli_v1'}}}})
        if self.path == '/local-cli/status':
            return self.reply({'ok': True, 'session_id': SID, 'command_reconciliation': None})
        return self.reply({'detail': 'not found'}, 404)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.loads(data) if 'application/json' in self.headers.get('Content-Type', '') else None
        type(self).calls.append(('POST', self.path, body))
        if self.path == '/local-cli/open':
            return self.reply({'ok': True, 'session_id': SID, 'working_copy_id': SID, 'source_filename': 'fixture.hwpx'})
        if self.path in ['/local-cli/where', '/local-cli/find', '/local-cli/save', '/local-cli/close', '/local-cli/command-bundle', '/local-cli/command-reconcile', '/local-cli/screenshot']:
            if type(self).mode == 'delay':
                time.sleep(2)
            if type(self).mode == 'failure':
                return self.reply({'detail': {'code': 'NATIVE_BLOCKED', 'message': 'blocked', 'command_id': 'cmd-owned'}}, 409)
            return self.reply({'ok': True, 'session_id': SID, 'command_id': 'cmd-owned', 'state': 'succeeded'})
        return self.reply({'detail': 'not found'}, 404)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hwpx-mcp-tests-')
        cls.root = Path(cls.tmp.name)
        cls.fixture = cls.root / 'fixture.hwpx'
        cls.fixture.write_bytes(b'disposable mock bytes: not native HWPX proof')
        cls.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        cls.thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = port()
        cls.url = f'http://127.0.0.1:{cls.port}/mcp'
        cls.token = secrets.token_urlsafe(32)
        env = os.environ.copy()
        env.update(HWPX_MCP_TOKEN=cls.token, HWPX_MCP_PORT=str(cls.port),
                   HWPX_MCP_BACKEND=f'http://127.0.0.1:{cls.backend.server_port}',
                   HWPX_MCP_SOURCE_ROOT=str(cls.root), HWPX_MCP_ARTIFACT_ROOT=str(cls.root / 'artifacts'),
                   HWPX_MCP_TIMEOUT='0.5')
        cls.log = (cls.root / 'server.log').open('wb')
        cls.proc = subprocess.Popen([sys.executable, '-m', 'hwpx_mcp'], env=env, stdout=cls.log, stderr=cls.log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                cls.log.flush()
                raise RuntimeError((cls.root / 'server.log').read_text())
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.2):
                    return
            except OSError:
                time.sleep(.05)
        raise RuntimeError('adapter did not listen')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.log.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        Backend.calls = []
        Backend.mode = 'normal'

    def client(self):
        return Client(streamable_http_client(self.url, http_client=self.http), mode=VERSION)

    async def sdk_call(self, name, args):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}) as self.http:
            async with self.client() as client:
                return await client.call_tool(name, args)

    async def raw(self, method='tools/list', params=None, headers=None, body=None):
        p = {'_meta': dict(META)} if params is None else params
        h = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json, text/event-stream',
             'Content-Type': 'application/json', 'MCP-Protocol-Version': VERSION, 'Mcp-Method': method}
        if method == 'tools/call' and 'name' in p:
            h['Mcp-Name'] = p['name']
        if headers:
            for key, value in headers.items():
                if value is None:
                    h.pop(key, None)
                else:
                    h[key] = value
        raw = body if body is not None else json.dumps({'jsonrpc': '2.0', 'id': 7, 'method': method, 'params': p})
        async with httpx2.AsyncClient(timeout=5) as http:
            response = await http.post(self.url, content=raw, headers=h)
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
            payload = next(x for x in messages if 'result' in x or 'error' in x)
        else:
            payload = response.json() if response.content and 'application/json' in response.headers.get('content-type', '') else None
        return response, payload

    async def test_discovery_tools_schemas(self):
        response, payload = await self.raw('server/discover')
        self.assertEqual(response.status_code, 200)
        result = payload['result']
        self.assertEqual(result['supportedVersions'], [VERSION])
        self.assertEqual(set(result['capabilities']), {'tools'})
        response, payload = await self.raw()
        tools = payload['result']['tools']
        self.assertEqual([t['name'] for t in tools], sorted('hwpx_' + x for x in ['health','open','status','find','where','command','proof','save','close']))
        for tool in tools:
            Draft202012Validator.check_schema(tool['inputSchema'])
            Draft202012Validator.check_schema(tool['outputSchema'])
            self.assertIs(tool['inputSchema']['additionalProperties'], False)
            self.assertIs(tool['annotations']['openWorldHint'], False)

    async def test_official_client_success(self):
        result = await self.sdk_call('hwpx_health', {})
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content['ok'])
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    async def test_lifecycle_identity_and_source_preserved(self):
        before = self.fixture.read_bytes()
        result = await self.sdk_call('hwpx_open', {'request': {'source_path': str(self.fixture)}})
        self.assertEqual(result.structured_content['session_id'], SID)
        self.assertEqual(result.structured_content['document_id'], SID)
        for name in ['status', 'where', 'save', 'close', 'status']:
            result = await self.sdk_call('hwpx_' + name, {'session_id': SID})
            self.assertFalse(result.is_error, result)
            self.assertEqual(result.structured_content['session_id'], SID)
        self.assertEqual(before, self.fixture.read_bytes())

    async def test_missing_session_rejected_before_backend(self):
        result = await self.sdk_call('hwpx_where', {})
        self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_invalid_sessions_rejected_before_backend(self):
        for sid in ['', ' ', '../a', 'x?session_id=other', 1]:
            with self.subTest(sid=sid):
                result = await self.sdk_call('hwpx_where', {'session_id': sid})
                self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_nested_session_rejected(self):
        result = await self.sdk_call('hwpx_find', {'session_id': SID, 'request': {'query': 'x', 'session_id': OTHER}})
        self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_extra_arguments_rejected(self):
        result = await self.sdk_call('hwpx_health', {'backend_url': 'http://evil'})
        self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_session_isolation(self):
        result = await self.sdk_call('hwpx_close', {'session_id': OTHER})
        self.assertTrue(result.is_error)
        self.assertFalse(any(method == 'POST' for method, _, _ in Backend.calls))

    async def test_backend_failure_not_success(self):
        Backend.mode = 'failure'
        result = await self.sdk_call('hwpx_where', {'session_id': SID})
        self.assertTrue(result.is_error)
        self.assertFalse(result.structured_content['ok'])
        self.assertEqual(result.structured_content['error']['code'], 'BACKEND_REJECTED')
        self.assertIn('cmd-owned', json.dumps(result.structured_content))

    async def test_backend_timeout_is_ambiguous_not_rollback(self):
        Backend.mode = 'delay'
        result = await self.sdk_call('hwpx_save', {'session_id': SID})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['code'], 'BACKEND_OUTCOME_UNKNOWN')
        self.assertIn('reconcile', result.structured_content['error']['message'])
        self.assertEqual(len([c for c in Backend.calls if c[0] == 'POST']), 1)

    async def test_arbitrary_command_rejected(self):
        for op in ['pyhwpx_call', 'hwp_action', 'shell', 'set_text_file']:
            result = await self.sdk_call('hwpx_command', {'session_id': SID, 'request': {'op': op}})
            self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_guarded_command_requires_preconditions(self):
        result = await self.sdk_call('hwpx_command', {'session_id': SID, 'request': {'op': 'cell_format_exact', 'fill_color': 0}})
        self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_find_constraints(self):
        for request in [{'query':'x','around':6}, {'query':'x','proof_match':0}]:
            result = await self.sdk_call('hwpx_find', {'session_id': SID, 'request': request})
            self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_proof_bounds(self):
        for request in [{'kind':'page','page':0}, {'kind':'page','page':1,'dpi':1201}, {'kind':'page','page':10001}]:
            result = await self.sdk_call('hwpx_proof', {'session_id': SID, 'request': request})
            self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_source_escape_rejected(self):
        result = await self.sdk_call('hwpx_open', {'request': {'source_path': str(self.root.parent / 'not-owned.hwpx')}})
        self.assertTrue(result.is_error)
        self.assertEqual(Backend.calls, [])

    async def test_auth_required(self):
        for value in [None, 'Bearer invalid']:
            response, _ = await self.raw(headers={'Authorization': value})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(Backend.calls, [])

    async def test_origin_and_host(self):
        for value in ['http://evil.invalid', 'null', '', 'http://localhost:1']:
            response, _ = await self.raw(headers={'Origin': value})
            self.assertEqual(response.status_code, 403)
        response, _ = await self.raw(headers={'Host': 'evil.invalid'})
        self.assertEqual(response.status_code, 403)
        response, _ = await self.raw(headers={'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual(response.status_code, 200)

    async def test_invalid_json(self):
        response, payload = await self.raw(body='{')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload['error']['code'], -32700)

    async def test_unknown_method(self):
        response, payload = await self.raw('unknown/method')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(payload['error']['code'], -32601)

    async def test_unknown_tool(self):
        response, payload = await self.raw('tools/call', {'_meta': META, 'name': 'unknown', 'arguments': {}})
        self.assertEqual(payload['error']['code'], -32602)

    async def test_missing_body_metadata(self):
        response, payload = await self.raw(params={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload['error']['code'], -32602)

    async def test_missing_capabilities(self):
        response, payload = await self.raw(params={'_meta': {'io.modelcontextprotocol/protocolVersion': VERSION}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload['error']['code'], -32602)

    async def test_header_metadata_mismatch(self):
        for headers in [{'MCP-Protocol-Version': None}, {'Mcp-Method': None}, {'Mcp-Method': 'wrong'}, {'MCP-Protocol-Version': '2025-11-25'}]:
            response, payload = await self.raw(headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(payload['error']['code'], -32020)

    async def test_unsupported_version(self):
        unknown = '2099-01-01'
        response, payload = await self.raw(params={'_meta': {**META, 'io.modelcontextprotocol/protocolVersion': unknown}}, headers={'MCP-Protocol-Version': unknown})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload['error']['code'], -32022)
        self.assertEqual(payload['error']['data']['supported'], [VERSION])

    async def test_mcp_name_mismatch(self):
        response, payload = await self.raw('tools/call', {'_meta': META, 'name': 'hwpx_health', 'arguments': {}}, headers={'Mcp-Name': 'hwpx_save'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload['error']['code'], -32020)

    async def test_modern_only_http_methods(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer '+self.token}) as http:
            for method in ['GET', 'DELETE']:
                response = await http.request(method, self.url)
                self.assertEqual(response.status_code, 405)

    async def test_parallel_health(self):
        results = await asyncio.gather(*(self.raw('tools/call', {'_meta': META, 'name': 'hwpx_health', 'arguments': {}}) for _ in range(6)))
        self.assertTrue(all(r.status_code == 200 for r, _ in results))

    async def test_successful_bounded_commands(self):
        for op in ['context', 'selection_proof', 'readback']:
            result = await self.sdk_call('hwpx_command', {'session_id': SID, 'request': {'op': op}})
            self.assertFalse(result.is_error, result)
            post = [x for x in Backend.calls if x[0] == 'POST'][-1]
            self.assertEqual(post[1], '/local-cli/command-bundle')
            self.assertEqual(post[2]['session_id'], SID)
            self.assertEqual(post[2]['steps'][0]['op'], op)

    async def test_reconcile_preserves_identifier(self):
        result = await self.sdk_call('hwpx_command', {'session_id': SID,
            'request': {'op': 'command_reconcile', 'command_id': 'cmd-owned'}})
        self.assertFalse(result.is_error)
        self.assertEqual(Backend.calls[-1], ('POST', '/local-cli/command-reconcile',
                         {'session_id': SID, 'command_id': 'cmd-owned'}))

    async def test_backend_false_http_200(self):
        from hwpx_mcp.server import Facade, Settings, BackendFailure
        async def handler(request):
            return httpx2.Response(200, json={'ok': False, 'command_id': 'cmd-rejected'})
        settings = Settings(self.token, self.root, self.root / 'artifacts', port=self.port)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
            with self.assertRaises(BackendFailure) as found:
                await Facade(settings).request(http, 'POST', '/local-cli/save')
        self.assertEqual(found.exception.details['backend']['command_id'], 'cmd-rejected')

    async def test_invalid_capability_shape(self):
        for value in [None, [], 'tools']:
            response, payload = await self.raw(params={'_meta': {**META, 'io.modelcontextprotocol/clientCapabilities': value}})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(payload['error']['code'], -32602)

    async def test_malformed_envelopes(self):
        for value in [[], {'jsonrpc':'2.0','id':None,'method':'tools/list','params':{'_meta':META}},
                      {'jsonrpc':'2.0','id':1,'result':{}}, {'jsonrpc':'1.0','id':1,'method':'tools/list'}]:
            response, payload = await self.raw(body=json.dumps(value))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(payload['error']['code'], -32600)

    async def test_media_types(self):
        for accept in ['application/json', 'text/event-stream', 'text/plain']:
            response, _ = await self.raw(headers={'Accept': accept})
            self.assertEqual(response.status_code, 406)
        response, _ = await self.raw(headers={'Content-Type': 'text/plain'})
        self.assertEqual(response.status_code, 415)

    async def test_request_size_bounded(self):
        response, _ = await self.raw(body=' ' * (1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)

    async def test_sdk_cancellation_does_not_replay(self):
        Backend.mode = 'delay'
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}) as self.http:
            async with self.client() as client:
                task = asyncio.create_task(client.call_tool('hwpx_save', {'session_id': SID}))
                deadline = time.monotonic() + 2
                while not any(c[0] == 'POST' for c in Backend.calls) and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                self.assertTrue(any(c[0] == 'POST' for c in Backend.calls))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        Backend.mode = 'normal'
        result = await self.sdk_call('hwpx_health', {})
        self.assertFalse(result.is_error)
        self.assertEqual(len([c for c in Backend.calls if c[0] == 'POST']), 1)

    async def test_invalid_page_proof_error_is_structured(self):
        # A non-PDF export is an explicit tool failure, never a protocol crash.
        result = await self.sdk_call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'page', 'page': 1}})
        self.assertTrue(result.is_error)
        self.assertFalse(result.structured_content['ok'])

    def test_page_renderer_is_bounded_and_preserves_page(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from hwpx_mcp.proof import render_page
        from PIL import Image
        pdf = self.root / 'render-input.pdf'
        pdf.write_bytes(b'%PDF-mocked-renderer-input')
        out = self.root / 'render-output.png'
        def run(argv, **kwargs):
            self.assertEqual(kwargs['timeout'], 60)
            self.assertEqual(argv[argv.index('-f') + 1], '3')
            self.assertEqual(argv[argv.index('-l') + 1], '3')
            Image.new('RGB', (100, 200), 'white').save(argv[-1] + '-3.png')
        with patch('hwpx_mcp.proof.resolve_pdftoppm', return_value=SimpleNamespace(ok=True, path=Path('pdftoppm.exe'))), patch('hwpx_mcp.proof.subprocess.run', side_effect=run):
            render_page(pdf, out, page=3, dpi=160)
        self.assertTrue(out.is_file())

    def test_page_renderer_timeout_is_named_failure(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from hwpx_mcp.proof import render_page, ProofError
        with patch('hwpx_mcp.proof.resolve_pdftoppm', return_value=SimpleNamespace(ok=True, path=Path('pdftoppm.exe'))), patch('hwpx_mcp.proof.subprocess.run', side_effect=subprocess.TimeoutExpired('pdftoppm', 60)):
            with self.assertRaises(ProofError):
                render_page(self.root/'input.pdf', self.root/'missing.png', page=1, dpi=160)

    async def test_documented_client_entrypoint(self):
        env = os.environ.copy()
        env['HWPX_MCP_TOKEN'] = self.token
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-m', 'hwpx_mcp.client', '--url', self.url, '--tool', 'hwpx_health'],
            env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['structuredContent']['ok'])
        self.assertNotIn(self.token.encode(), result.stdout + result.stderr)

    async def test_config_rejects_unsafe_values(self):
        from hwpx_mcp.server import Settings
        for changes in [{'token': 'x'}, {'port': 0}, {'timeout': 0}, {'backend': 'http://example.org'},
                        {'backend': 'http://user:password@localhost'}, {'backend': 'http://localhost/?token=x'},
                        {'backend_token': self.token}, {'token': 'é' * 32}]:
            fields = dict(token=self.token, source_root=self.root, artifact_root=self.root/'artifacts', port=self.port)
            fields.update(changes)
            with self.assertRaises(ValueError):
                Settings(**fields)


if __name__ == '__main__':
    unittest.main(verbosity=2)
