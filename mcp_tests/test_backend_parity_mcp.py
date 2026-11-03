"""Backend-to-MCP parity through the real MCP transport and an isolated fake backend.

Every exposed backend capability is called with the official SDK client; the
fake backend records exactly what reached it. No native application is used.
"""
from __future__ import annotations

import email.parser
import email.policy
import hashlib
import io
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
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
from jsonschema import Draft202012Validator
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parity_samples as samples  # noqa: E402

VERSION = '2026-07-28'
SID = 'c' * 32
JOB = 'd' * 32
FOREIGN_JOB = 'e' * 32
ROOT = Path(__file__).resolve().parents[1]


def tiny_pdf(pages=2):
    """A minimal valid PDF with blank pages, so the real Poppler path renders."""
    objects = ['<< /Type /Catalog /Pages 2 0 R >>']
    kids = ' '.join(f'{3 + i} 0 R' for i in range(pages))
    objects.append(f'<< /Type /Pages /Kids [{kids}] /Count {pages} >>')
    for _ in range(pages):
        objects.append('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 280] >>')
    out = io.BytesIO()
    out.write(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f'{number} 0 obj\n{body}\nendobj\n'.encode())
    xref = out.tell()
    out.write(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets:
        out.write(f'{offset:010d} 00000 n \n'.encode())
    out.write(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return out.getvalue()


def tiny_hwpx():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('mimetype', 'application/hwp+zip')
        package.writestr('Contents/section0.xml', '<sec>편집됨</sec>')
    return archive.getvalue()


PDF = tiny_pdf()
EDITED = tiny_hwpx()
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
RECOVERY = b'recovery bytes'


def multipart(headers, body):
    raw = f'Content-Type: {headers["Content-Type"]}\r\n\r\n'.encode() + body
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    form, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if part.get_filename():
            files[name] = (part.get_filename(), part.get_payload(decode=True))
        else:
            form[name] = part.get_payload(decode=True).decode('utf-8')
    return form, files


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
        self.wfile.write(raw)

    def raw_reply(self, raw, content_type):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        type(self).calls.append(('GET', self.path, None))
        if self.path.startswith('/interactive/session/status?session_id='):
            return self.reply({'ok': True, 'session': {'session_id': SID, 'state': 'idle',
                               'metadata': {'local_cli_v1': {'opened_via': 'local_cli_v1'}}}})
        if self.path == '/local-cli/status':
            return self.reply({'ok': True, 'session_id': SID, 'working_copy_dirty': False})
        if self.path == f'/local-cli/session/{SID}/artifact/screenshot':
            return self.raw_reply(PNG, 'image/png')
        if self.path == f'/local-cli/session/{SID}/artifact/recovery':
            return self.raw_reply(RECOVERY, 'application/octet-stream')
        if self.path == '/capabilities':
            return self.reply({'ok': True, 'supported_ops': ['replace_all'], 'queued_job_unsupported_ops': ['official_numbering', 'page_number']})
        if self.path == '/runtime-readiness':
            return self.reply({'ok': True, 'ready': True})
        if self.path == f'/jobs/{JOB}':
            state = 'running' if type(self).mode == 'job_running' else 'succeeded'
            return self.reply({'ok': True, 'job': {'job_id': JOB, 'status': state}})
        if self.path == f'/jobs/{JOB}/result':
            return self.raw_reply(PDF, 'application/pdf')
        if self.path == f'/jobs/{JOB}/edited':
            return self.raw_reply(EDITED, 'application/zip')
        if self.path == f'/jobs/{JOB}/edit-summary':
            return self.reply({'operations': [{'op': 'replace_text_safe', 'matches': 1}]})
        if self.path in (f'/jobs/{JOB}/confirm-state', f'/jobs/{JOB}/confirm-brief'):
            return self.reply({'ok': True, 'job_id': JOB, 'state': {'status': 'awaiting_confirmation'}, 'brief': 'b'})
        return self.reply({'detail': 'not found'}, 404)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        content_type = self.headers.get('Content-Type', '')
        if content_type.startswith('multipart/form-data'):
            form, files = multipart(self.headers, data)
            body = {'form': form, 'files': {k: (v[0], hashlib.sha256(v[1]).hexdigest()) for k, v in files.items()}}
        else:
            body = json.loads(data) if data else None
        type(self).calls.append(('POST', self.path, body))
        if type(self).mode == 'delay':
            time.sleep(2)
        if self.path == '/local-cli/command-bundle':
            if type(self).mode == 'step_refusal':
                return self.reply({'ok': False, 'semantic_ok': False, 'session_id': SID, 'dirty': False,
                                   'steps': [{'index': 1, 'op': 'layout_exact', 'ok': False, 'refusal_code': 'target_not_found',
                                              'refusal_message': 'not found', 'mutation_may_have_persisted': False}]})
            if type(self).mode == 'technical':
                return self.reply({'ok': False, 'semantic_ok': False, 'session_id': SID, 'dirty': True, 'native_custody_held': True,
                                   'steps': [{'index': 1, 'op': 'control_move_resize_exact', 'ok': False,
                                              'mutation_may_have_persisted': True, 'error': 'LocalCliMutationError'}]})
            return self.reply({'ok': True, 'session_id': SID, 'steps': [{'index': i + 1, 'op': s['op'], 'ok': True}
                                                                         for i, s in enumerate(body['steps'])]})
        if self.path == '/local-cli/screenshot':
            return self.reply({'ok': True, 'session_id': SID, 'download_path': f'/local-cli/session/{SID}/artifact/screenshot'})
        if self.path.startswith('/local-cli/'):
            return self.reply({'ok': True, 'session_id': SID, 'command': self.path.rsplit('/', 1)[-1]})
        if self.path in ('/edit-and-convert', '/convert', '/author-and-convert', '/clear-placeholders-and-convert',
                         '/fill-placeholders-and-convert'):
            return self.reply({'ok': True, 'job': {'job_id': JOB, 'status': 'queued'}})
        if self.path in ('/compile-authoring', '/clear-placeholders', '/compile-placeholder-fills'):
            return self.reply({'ok': True, 'job_id': JOB, 'compile_readiness': {'status': 'ready'}})
        if self.path == f'/jobs/{JOB}/confirm-action':
            return self.reply({'ok': True, 'job_id': JOB, 'state': {'status': 'approved'}})
        return self.reply({'detail': 'not found'}, 404)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ParityTransport(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hwpx-mcp-parity-')
        cls.root = Path(cls.tmp.name)
        cls.inputs = cls.root / 'input'
        cls.inputs.mkdir()
        cls.artifacts = cls.root / 'artifacts'
        cls.fixture = cls.inputs / 'source.hwpx'
        cls.fixture.write_bytes(tiny_hwpx())
        cls.picture = cls.inputs / 'picture.png'
        cls.picture.write_bytes(PNG)
        (cls.inputs / 'animated.gif').write_bytes(b'GIF89a')
        cls.outside = cls.root / 'outside.png'
        cls.outside.write_bytes(PNG)
        cls.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        cls.thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = free_port()
        cls.url = f'http://127.0.0.1:{cls.port}/mcp'
        cls.token = secrets.token_urlsafe(32)
        env = os.environ.copy()
        env.update(HWPX_MCP_TOKEN=cls.token, HWPX_MCP_PORT=str(cls.port),
                   HWPX_MCP_BACKEND=f'http://127.0.0.1:{cls.backend.server_port}',
                   HWPX_MCP_SOURCE_ROOT=str(cls.inputs), HWPX_MCP_ARTIFACT_ROOT=str(cls.artifacts),
                   HWPX_MCP_TIMEOUT='1')
        cls.log = (cls.root / 'server.log').open('wb')
        cls.proc = subprocess.Popen([sys.executable, '-m', 'hwpx_mcp'], env=env, stdout=cls.log, stderr=cls.log, cwd=ROOT)
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

    async def call(self, name, args):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                return await client.call_tool(name, args)

    async def tools(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                return (await client.list_tools()).tools

    def posts(self, path=None):
        return [c for c in Backend.calls if c[0] == 'POST' and (path is None or c[1] == path)]

    async def test_new_tools_are_discoverable_with_valid_closed_schemas(self):
        tools = {tool.name: tool for tool in await self.tools()}
        for name in ('hwpx_bundle', 'hwpx_image', 'hwpx_job', 'hwpx_artifact', 'hwpx_capabilities'):
            self.assertIn(name, tools)
        for tool in tools.values():
            Draft202012Validator.check_schema(tool.input_schema)
            self.assertIs(tool.input_schema['additionalProperties'], False)
            self.assertTrue(tool.description)
        command = json.dumps(tools['hwpx_command'].input_schema)
        bundle = json.dumps(tools['hwpx_bundle'].input_schema)
        for op in samples.BUNDLE_SAMPLES:
            self.assertIn(f'"const": "{op}"', command, op)
            self.assertIn(f'"const": "{op}"', bundle, op)
        for op in samples.REST_SAMPLES:
            self.assertIn(f'"const": "{op}"', command, op)
        job = json.dumps(tools['hwpx_job'].input_schema)
        for op in samples.QUEUED_SAMPLES:
            self.assertIn(f'"const": "{op}"', job, op)
        for op in samples.QUEUED_REFUSALS:
            self.assertNotIn(f'"const": "{op}"', job, op)

    async def test_every_exposed_bundle_op_reaches_the_backend_with_exact_fields(self):
        for op, variants in sorted(samples.BUNDLE_SAMPLES.items()):
            for sample in variants:
                with self.subTest(op=op, sample=sample):
                    Backend.calls = []
                    result = await self.call('hwpx_command', {'session_id': SID, 'request': {'op': op, **sample}})
                    self.assertFalse(result.is_error, result.structured_content)
                    posts = self.posts('/local-cli/command-bundle')
                    self.assertEqual(len(posts), 1)
                    body = posts[0][2]
                    self.assertEqual(body['session_id'], SID)
                    self.assertEqual(len(body['steps']), 1)
                    step = dict(body['steps'][0])
                    self.assertEqual(step.pop('op'), op)
                    self.assertTrue(step.pop('label'))
                    expected = json.loads(json.dumps(sample))
                    for key, value in step.items():
                        if key not in expected:
                            expected[key] = value  # documented MCP defaults are forwarded explicitly
                    self.assertEqual(step, expected)

    async def test_refused_requests_never_reach_the_backend(self):
        for op, variants in sorted(samples.BUNDLE_REFUSALS.items()):
            for sample in variants:
                with self.subTest(op=op, sample=sample):
                    result = await self.call('hwpx_command', {'session_id': SID, 'request': {'op': op, **sample}})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
        self.assertEqual(self.posts(), [])

    async def test_multi_step_bundle_is_one_backend_request_in_order(self):
        steps = [{'op': 'where'}, {'op': 'anchor_insert', **samples.BUNDLE_SAMPLES['anchor_insert'][0]},
                 {'op': 'readback', 'scope': 'caret'}]
        result = await self.call('hwpx_bundle', {'session_id': SID, 'request': {'steps': steps}})
        self.assertFalse(result.is_error, result.structured_content)
        posts = self.posts('/local-cli/command-bundle')
        self.assertEqual(len(posts), 1)
        self.assertEqual([s['op'] for s in posts[0][2]['steps']], ['where', 'anchor_insert', 'readback'])
        self.assertEqual([s['op'] for s in result.structured_content['result']['steps']], ['where', 'anchor_insert', 'readback'])

    async def test_bundle_refusal_and_technical_failure_keep_backend_evidence(self):
        Backend.mode = 'step_refusal'
        result = await self.call('hwpx_bundle', {'session_id': SID, 'request': {'steps': [{'op': 'layout_inspect'}]}})
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual(details['reason'], 'target_not_found')
        self.assertTrue(details['stopped_before_edit'])
        Backend.mode = 'technical'
        Backend.calls = []
        sample = samples.BUNDLE_SAMPLES['control_move_resize_exact'][0]
        result = await self.call('hwpx_command', {'session_id': SID, 'request': {'op': 'control_move_resize_exact', **sample}})
        self.assertTrue(result.is_error)
        self.assertTrue(result.structured_content['error']['details']['session_held'])
        self.assertEqual(len(self.posts()), 1, 'no follow-up native call after an uncertain result')

    async def test_direct_session_routes_are_forwarded_exactly(self):
        for op, (sample, path) in sorted(samples.REST_SAMPLES.items()):
            with self.subTest(op=op):
                Backend.calls = []
                result = await self.call('hwpx_command', {'session_id': SID, 'request': {'op': op, **sample}})
                self.assertFalse(result.is_error, result.structured_content)
                posts = self.posts(path)
                self.assertEqual(len(posts), 1, Backend.calls)
                self.assertEqual(posts[0][2], {'session_id': SID, **sample})

    async def test_image_routes_upload_only_confined_pictures(self):
        digest = hashlib.sha256(PNG).hexdigest()
        result = await self.call('hwpx_image', {'session_id': SID, 'request': {
            'action': 'insert', 'source_path': str(self.picture), 'width': 40.0, 'treat_as_char': False}})
        self.assertFalse(result.is_error, result.structured_content)
        (path, _method, body), = [(c[1], c[0], c[2]) for c in self.posts()]
        self.assertEqual(path, '/local-cli/image')
        self.assertEqual(body['files']['file'], ('picture.png', digest))
        self.assertEqual(body['form'], {'session_id': SID, 'width': '40.0', 'treat_as_char': 'off'})
        Backend.calls = []
        result = await self.call('hwpx_image', {'session_id': SID, 'request': {
            'action': 'insert_at_anchor', 'source_path': str(self.picture), 'target': '기준', 'position': 'after-anchor', 'fit_cell': True}})
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(self.posts()[0][2]['form'], {'session_id': SID, 'target': '기준', 'position': 'after-anchor', 'fit_cell': 'true'})
        Backend.calls = []
        result = await self.call('hwpx_image', {'session_id': SID, 'request': {
            'action': 'figure_section', 'source_path': str(self.picture), 'target_heading': '1. 개요', 'heading': '그림',
            'caption': '그림 1', 'embedded': True}})
        self.assertFalse(result.is_error, result.structured_content)
        posted = self.posts()[0]
        self.assertEqual(posted[1], '/local-cli/figure-section-image')
        self.assertEqual(posted[2]['files']['image'], ('picture.png', digest))
        self.assertEqual(posted[2]['form'], {'session_id': SID, 'target_heading': '1. 개요', 'heading': '그림', 'caption': '그림 1', 'embedded': 'on'})
        Backend.calls = []
        for bad in (str(self.outside), str(self.inputs / 'animated.gif'), 'relative.png'):
            result = await self.call('hwpx_image', {'session_id': SID, 'request': {'action': 'insert', 'source_path': bad}})
            self.assertTrue(result.is_error, bad)
        self.assertEqual(self.posts(), [])

    async def test_queued_edit_job_submit_status_and_verified_retrieval(self):
        operations = [{'op': op, **sample} for op, sample in sorted(samples.QUEUED_SAMPLES.items())]
        result = await self.call('hwpx_job', {'request': {'action': 'edit_and_convert', 'source_path': str(self.fixture),
                                                           'instructions': {'operations': operations, 'validation': {'preserve_page_count': True}}}})
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(result.structured_content['result']['job']['job_id'], JOB)
        (path, body), = [(c[1], c[2]) for c in self.posts()]
        self.assertEqual(path, '/edit-and-convert')
        self.assertEqual(body['files']['file'], ('source.hwpx', hashlib.sha256(self.fixture.read_bytes()).hexdigest()))
        sent = json.loads(body['form']['instructions_json'])
        self.assertEqual(sent, {'operations': operations, 'validation': {'preserve_page_count': True}})
        status = await self.call('hwpx_job', {'request': {'action': 'status', 'job_id': JOB}})
        self.assertFalse(status.is_error, status.structured_content)
        summary = await self.call('hwpx_job', {'request': {'action': 'metadata', 'job_id': JOB, 'kind': 'edit_summary'}})
        self.assertFalse(summary.is_error, summary.structured_content)
        self.assertEqual(summary.structured_content['result']['metadata']['operations'][0]['matches'], 1)
        retrieved = await self.call('hwpx_job', {'request': {'action': 'retrieve', 'job_id': JOB, 'dpi': 72}})
        self.assertFalse(retrieved.is_error, retrieved.structured_content)
        value = retrieved.structured_content['result']
        self.assertEqual(Path(value['pdf']['path']).read_bytes(), PDF)
        self.assertEqual(value['pdf']['sha256'], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(Path(value['edited']['path']).read_bytes(), EDITED)
        self.assertEqual(len(value['pages']), 2)
        for page in value['pages']:
            self.assertTrue(Path(page['path']).is_file())
            self.assertTrue(Path(page['path']).resolve().is_relative_to(self.artifacts.resolve()))
        self.assertIn('compared_with_source', value['edited'])

    async def test_job_ownership_and_refusals(self):
        result = await self.call('hwpx_job', {'request': {'action': 'status', 'job_id': FOREIGN_JOB}})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['code'], 'JOB_NOT_OWNED')
        for op, sample in samples.QUEUED_REFUSALS.items():
            result = await self.call('hwpx_job', {'request': {'action': 'edit_and_convert', 'source_path': str(self.fixture),
                                                               'instructions': {'operations': [{'op': op, **sample}]}}})
            self.assertTrue(result.is_error, op)
            self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
        result = await self.call('hwpx_job', {'request': {'action': 'convert', 'source_path': str(self.root / 'escape.hwpx')}})
        self.assertTrue(result.is_error)
        self.assertEqual([c for c in Backend.calls if c[1].startswith('/jobs/') or c[0] == 'POST'], [])

    async def test_template_authoring_and_placeholder_flows(self):
        await self.call('hwpx_job', {'request': {'action': 'convert', 'source_path': str(self.fixture)}})
        result = await self.call('hwpx_job', {'request': {'action': 'compile_authoring', 'source_path': str(self.fixture),
                                                           'authoring_markdown': '# 제목\n\n본문', 'cleanup_placeholders': 'unused',
                                                           'policy_override': {'decision_override': 'confirm'}}})
        self.assertFalse(result.is_error, result.structured_content)
        posted = self.posts('/compile-authoring')[0][2]['form']
        self.assertEqual(posted['authoring_markdown'], '# 제목\n\n본문')
        self.assertEqual(posted['cleanup_placeholders'], 'unused')
        self.assertEqual(json.loads(posted['policy_override_json']), {'decision_override': 'confirm'})
        state = await self.call('hwpx_job', {'request': {'action': 'confirm_state', 'job_id': JOB}})
        self.assertFalse(state.is_error, state.structured_content)
        approved = await self.call('hwpx_job', {'request': {'action': 'confirm_action', 'job_id': JOB, 'confirm': 'approve', 'enqueue_convert': True}})
        self.assertFalse(approved.is_error, approved.structured_content)
        self.assertEqual(self.posts(f'/jobs/{JOB}/confirm-action')[0][2], {'action': 'approve', 'enqueue_convert': True})
        fills = {'placeholders': {'NAME': '홍길동'}, 'tables': [{'name': '표1', 'cells': {'r2c2': '값'}}]}
        result = await self.call('hwpx_job', {'request': {'action': 'fill_placeholders_and_convert', 'source_path': str(self.fixture),
                                                           'placeholder_fills': fills, 'validation': {'max_page_count': 3}}})
        self.assertFalse(result.is_error, result.structured_content)
        posted = self.posts('/fill-placeholders-and-convert')[0][2]['form']
        self.assertEqual(json.loads(posted['placeholder_fills_json']), fills)
        self.assertEqual(json.loads(posted['validation_json']), {'max_page_count': 3})

    async def test_running_job_is_not_downloaded(self):
        await self.call('hwpx_job', {'request': {'action': 'convert', 'source_path': str(self.fixture)}})
        Backend.mode = 'job_running'
        result = await self.call('hwpx_job', {'request': {'action': 'retrieve', 'job_id': JOB}})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['code'], 'JOB_NOT_READY')
        self.assertFalse(any(c[1].endswith('/result') for c in Backend.calls))

    async def test_job_submit_timeout_is_unknown_and_not_replayed(self):
        Backend.mode = 'delay'
        result = await self.call('hwpx_job', {'request': {'action': 'convert', 'source_path': str(self.fixture)}})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['code'], 'BACKEND_OUTCOME_UNKNOWN')
        self.assertEqual(len(self.posts('/convert')), 1)

    async def test_session_artifacts_and_frame_are_retrieved_into_the_artifact_root(self):
        for kind, raw in (('screenshot', PNG), ('recovery', RECOVERY)):
            result = await self.call('hwpx_artifact', {'session_id': SID, 'request': {'kind': kind}})
            self.assertFalse(result.is_error, result.structured_content)
            value = result.structured_content['result']
            self.assertEqual(Path(value['path']).read_bytes(), raw)
            self.assertEqual(value['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertTrue(Path(value['path']).resolve().is_relative_to(self.artifacts.resolve()))
        frame = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'frame'}})
        self.assertFalse(frame.is_error, frame.structured_content)
        self.assertEqual(Path(frame.structured_content['result']['frame_proof']['path']).read_bytes(), PNG)

    async def test_capabilities_classify_every_backend_operation(self):
        result = await self.call('hwpx_capabilities', {})
        self.assertFalse(result.is_error, result.structured_content)
        value = result.structured_content['result']
        manifests = {json.loads(p.read_text(encoding='utf-8'))['op'] for p in (ROOT / 'app/command_packages/commands').glob('*/manifest.json')}
        rows = {row['backend']: row for row in value['matrix']['bundle_ops']}
        self.assertEqual(set(rows), manifests)
        for op in samples.BUNDLE_SAMPLES:
            self.assertEqual(rows[op]['status'], 'exposed')
        self.assertEqual(value['backend']['capabilities']['queued_job_unsupported_ops'], ['official_numbering', 'page_number'])
        self.assertTrue(value['backend']['runtime_readiness']['ready'])
        self.assertIn('hwpx_command:anchor_insert', json.dumps(value['matrix']))
        # Native qualification is reported per variant, not once per op.
        self.assertIn('native_reference', value['matrix'])
        self.assertIn('Hancom Office 2024', value['matrix']['native_reference'])
        self.assertIn('variants', rows['layout_exact'])
        layout = {row['variant']: row for row in rows['layout_exact']['variants']}
        self.assertEqual((layout['section_insert:paragraph_start_or_end']['native'], layout['section_insert:paragraph_start_or_end']['refusal_code']),
                         ('unsupported', 'section_insert_position_unsupported'))
        self.assertEqual(layout['section_insert:inside_paragraph']['native'], 'qualified')
        objects = {row['variant']: row['native'] for row in rows['object_insert_exact']['variants']}
        self.assertEqual((objects['ellipse:inline'], objects['ellipse:floating']), ('qualified', 'not_qualified'))


if __name__ == '__main__':
    unittest.main()
