"""F02, F03, F11, F12 at the real MCP boundary: official SDK, sockets, a fake REST backend and real local Poppler.

Download failures here are controlled test faults. Not Hancom evidence.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

VERSION = '2026-07-28'
SID = 'e' * 32


def pdf_bytes(pages):
    objects = ['<< /Type /Catalog /Pages 2 0 R >>', None, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for lines in pages:
        text = ' '.join(f'1 0 0 1 72 {720 - 20 * n} Tm ({line}) Tj' for n, line in enumerate(lines))
        stream = f'BT /F1 12 Tf {text} ET'
        objects.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {len(objects)} 0 R /Resources << /Font << /F1 3 0 R >> >> >>')
        kids.append(f'{len(objects)} 0 R')
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'
    out = io.BytesIO(b'%PDF-1.4\n')
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f'{index} 0 obj\n{body}\nendobj\n'.encode())
    start = out.tell()
    out.write(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets:
        out.write(f'{offset:010d} 00000 n \n'.encode())
    out.write(f'trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return out.getvalue()


def hwpx_bytes(payload=b'image'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('Contents/section0.xml', '<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p><hp:tbl/><hp:pic/></hp:p></hs:sec>')
        archive.writestr('BinData/image1.png', payload)
    return out.getvalue()


def paragraph(text, heading='OUTLINE', level=0):
    return {'text': text, 'heading': heading, 'level': level, 'in_body': True, 'content_sha256': 'x' + text}


class Backend(BaseHTTPRequestHandler):
    calls = []
    saved = hwpx_bytes(b'edited')
    copy_mode = 'ok'  # ok, status500, truncated, corrupt, oversize, no_artifact
    dirty = False
    readback_mode = 'ok'
    close_mode = 'ok'
    printed = [['1. Alpha', '2. Beta'], ['second page']]

    def log_message(self, format, *args):
        pass

    def reply(self, payload, status=200, length=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/octet-stream' if isinstance(payload, bytes) else 'application/json')
        self.send_header('Content-Length', str(length if length is not None else len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        type(self).calls.append(('GET', self.path))
        if self.path.startswith('/interactive/session/status'):
            return self.reply({'ok': True, 'session': {'session_id': SID, 'state': 'ready', 'metadata': {'local_cli_v1': {'opened_via': 'local_cli_v1'}}}})
        if self.path == '/local-cli/status':
            return self.reply({'ok': True, 'session_id': SID, 'working_copy_dirty': type(self).dirty})
        if self.path.endswith('/artifact/export'):
            return self.reply(pdf_bytes(type(self).printed))
        if self.path.endswith('/artifact/working-copy'):
            mode = type(self).copy_mode
            if mode == 'status500':
                return self.reply({'detail': 'x'}, 500)
            if mode == 'no_artifact':
                return self.reply({'detail': 'none'}, 404)
            if mode == 'truncated':
                self.reply(type(self).saved[:40], length=len(type(self).saved))
                self.close_connection = True
                return None
            if mode == 'corrupt':
                return self.reply(b'PK\x03\x04 this is not a complete package')
            return self.reply(type(self).saved)
        return self.reply({'ok': True})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.loads(raw) if self.headers.get('Content-Type', '').startswith('application/json') else None
        type(self).calls.append(('POST', self.path, body))
        if self.path == '/local-cli/open':
            return self.reply({'ok': True, 'session_id': SID, 'working_copy_id': SID})
        if self.path == '/local-cli/numbering-readback':
            if type(self).readback_mode == 'unavailable':
                return self.reply({'ok': True, 'session_id': SID, 'available': False, 'reason': 'census_unavailable', 'source_filename': 'fixture.hwpx', 'page_count': 2, 'document_is_modified': False,
                                   'paragraphs': None, 'controls': {'tbl': 1}, 'page_numbers': [], 'page_restarts': [], 'evidence': None, 'caret_unchanged': True})
            return self.reply({'ok': True, 'session_id': SID, 'available': True, 'source_filename': 'fixture.hwpx', 'page_count': 2, 'document_is_modified': False,
                               'paragraphs': [paragraph('Alpha'), paragraph('Beta')], 'paragraph_total': 2, 'controls': {'tbl': 1}, 'page_numbers': [], 'page_restarts': [], 'caret_unchanged': True,
                               'evidence': {'header_footer': [], 'styles_sha256': 's', 'media_sha256': 'm', 'numberings_sha256': 'n', 'bindata_sha256': 'b'}})
        if self.path == '/local-cli/export':
            return self.reply({'ok': True, 'session_id': SID, 'download_path': f'/local-cli/session/{SID}/artifact/export'})
        if self.path == '/local-cli/save':
            return self.reply({'ok': True, 'session_id': SID, 'filename': 'fixture-edited.hwpx', 'download_path': f'/local-cli/session/{SID}/artifact/working-copy'})
        if self.path == '/local-cli/close':
            if type(self).close_mode == 'save_required':
                return self.reply({'detail': 'save_required: the document has unsaved changes; call hwpx_save first.'}, 409)
            return self.reply({'ok': True, 'cleanup': {'removed': True}})
        return self.reply({'ok': False}, 404)


class Base(unittest.IsolatedAsyncioTestCase):
    extra_env = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / 'input').mkdir()
        cls.source = cls.root / 'input' / 'fixture.hwpx'
        cls.source.write_bytes(hwpx_bytes(b'original'))
        cls.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{cls.port}/mcp'
        cls.token = secrets.token_urlsafe(32)
        env = os.environ.copy()
        env.update(HWPX_MCP_TOKEN=cls.token, HWPX_MCP_PORT=str(cls.port), HWPX_MCP_BACKEND=f'http://127.0.0.1:{cls.backend.server_port}',
                   HWPX_MCP_SOURCE_ROOT=str(cls.root / 'input'), HWPX_MCP_ARTIFACT_ROOT=str(cls.root / 'artifacts'), HWPX_MCP_TIMEOUT='20', **cls.env_for(cls.root))
        cls.log = (cls.root / 'server.log').open('wb')
        cls.proc = subprocess.Popen([sys.executable, '-m', 'hwpx_mcp'], env=env, stdout=cls.log, stderr=cls.log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.2):
                    return
            except OSError:
                time.sleep(.05)
        raise RuntimeError('adapter did not listen')

    @classmethod
    def env_for(cls, root):
        return {}

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.log.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        Backend.calls = []
        Backend.saved = hwpx_bytes(b'edited')
        Backend.copy_mode = 'ok'
        Backend.dirty = False
        Backend.readback_mode = 'ok'
        Backend.close_mode = 'ok'
        Backend.printed = [['1. Alpha', '2. Beta'], ['second page']]
        shutil.rmtree(self.root / 'artifacts', ignore_errors=True)
        self.saved_dir = self.root / 'artifacts' / SID / 'saved'

    async def call(self, name, args):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                return await client.call_tool(name, args)

    async def opened(self):
        result = await self.call('hwpx_open', {'request': {'source_path': str(self.source)}})
        self.assertFalse(result.is_error, result.structured_content)

    async def save(self):
        result = await self.call('hwpx_save', {'session_id': SID})
        self.assertFalse(result.is_error, result.structured_content)
        return result.structured_content['result']['saved_output']

    async def close(self):
        return await self.call('hwpx_close', {'session_id': SID})

    def close_posts(self):
        return [c for c in Backend.calls if c[0] == 'POST' and c[1] == '/local-cli/close']

    def published(self):
        return sorted(p.name for p in self.saved_dir.glob('*') if p.is_file()) if self.saved_dir.exists() else []


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class SavedOutputNeverLost(Base):
    async def test_a_failed_copy_never_reaches_the_backend_close(self):
        await self.opened()
        Backend.copy_mode = 'status500'
        await self.save()
        await self.close()
        self.assertEqual(self.close_posts(), [])

    async def test_close_is_refused_while_the_saved_file_cannot_be_retrieved(self):
        await self.opened()
        Backend.copy_mode = 'status500'
        saved = await self.save()
        self.assertFalse(saved['retrieved'])
        self.assertTrue(saved['close_blocked'])
        self.assertIn('hwpx_save', saved['retry'])
        result = await self.close()
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
        self.assertEqual(self.close_posts(), [])

    async def test_a_retry_recovers_and_the_ordinary_close_then_works(self):
        await self.opened()
        Backend.copy_mode = 'status500'
        await self.save()
        Backend.copy_mode = 'ok'
        saved = await self.save()
        self.assertTrue(saved['retrieved'])
        self.assertEqual(Path(saved['path']).read_bytes(), Backend.saved)
        result = await self.close()
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(result.structured_content['result']['saved_output']['path'], saved['path'])
        self.assertEqual(len(self.close_posts()), 1)

    async def test_close_itself_retrieves_a_saved_file_that_was_not_copied_yet(self):
        await self.opened()
        Backend.copy_mode = 'status500'
        await self.save()
        Backend.copy_mode = 'ok'
        result = await self.close()
        self.assertFalse(result.is_error, result.structured_content)
        saved = result.structured_content['result']['saved_output']
        self.assertEqual(Path(saved['path']).read_bytes(), Backend.saved)

    async def test_an_edited_working_copy_saved_behind_the_adapter_is_retrieved_before_close(self):
        await self.opened()
        result = await self.close()
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(Path(result.structured_content['result']['saved_output']['path']).read_bytes(), Backend.saved)

    async def test_unchanged_document_closes_without_a_copy(self):
        await self.opened()
        Backend.saved = self.source.read_bytes()
        result = await self.close()
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(self.published(), [])
        self.assertIsNone(result.structured_content['result']['saved_output'])

    async def test_a_second_failed_copy_marks_the_older_file_as_not_the_latest(self):
        await self.opened()
        first = await self.save()
        Backend.saved = hwpx_bytes(b'edited again')
        Backend.copy_mode = 'status500'
        second = await self.save()
        self.assertFalse(second['retrieved'])
        self.assertEqual(second['last_retrieved']['path'], first['path'])
        reviewed = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        value = reviewed.structured_content['result']['saved_output']
        self.assertFalse(value['retrieved'])
        self.assertNotIn('path', value)
        self.assertIn('최신 저장본', reviewed.structured_content['result']['summary_text'])
        refused = await self.close()
        self.assertEqual(refused.structured_content['error']['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')

    async def test_a_truncated_or_corrupt_copy_is_never_published_or_reopenable(self):
        for mode in ('truncated', 'corrupt'):
            with self.subTest(mode=mode):
                shutil.rmtree(self.root / 'artifacts', ignore_errors=True)
                await self.opened()
                Backend.copy_mode = mode
                saved = await self.save()
                self.assertFalse(saved['retrieved'])
                self.assertEqual([name for name in self.published() if name.lower().endswith(('.hwpx', '.hwp'))], [])
                self.assertEqual([p.name for p in self.saved_dir.glob('*')], [])

    async def test_no_artifact_after_a_recorded_save_refuses_close(self):
        await self.opened()
        Backend.copy_mode = 'status500'
        await self.save()
        Backend.copy_mode = 'no_artifact'
        refused = await self.close()
        self.assertEqual(refused.structured_content['error']['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
        self.assertEqual(self.close_posts(), [])

    async def test_no_artifact_without_any_save_keeps_the_ordinary_close(self):
        Backend.copy_mode = 'no_artifact'
        result = await self.close()
        self.assertFalse(result.is_error, result.structured_content)

    async def test_an_unsaved_document_gets_the_backend_save_reason_not_a_copy(self):
        await self.opened()
        Backend.dirty = True
        Backend.close_mode = 'save_required'
        result = await self.close()
        self.assertEqual(result.structured_content['error']['code'], 'SAVE_REQUIRED')
        self.assertEqual(self.published(), [])

    async def test_a_published_copy_is_complete_and_its_record_is_current(self):
        await self.opened()
        saved = await self.save()
        record = json.loads((self.root / 'artifacts' / SID / 'latest-saved.json').read_text(encoding='utf-8'))
        self.assertEqual(record['path'], saved['path'])
        self.assertEqual(self.published(), [Path(saved['path']).name])


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class ReviewAtTheBoundary(Base):
    async def review(self, **request):
        return await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0, **request}})

    async def test_labels_beyond_max_pages_are_not_called_wrong(self):
        Backend.printed = [['1. Alpha'], ['2. Beta']]
        value = (await self.review(max_pages=1)).structured_content['result']
        self.assertNotIn('기대와 다른', value['summary_text'])
        self.assertNotEqual(value['numbering_all_match'], False)

    async def test_complete_review_is_stated_as_complete(self):
        value = (await self.review()).structured_content['result']
        self.assertEqual(value['review_status'], 'complete')
        self.assertTrue(value['summary_text'].startswith('확인 결과: 전체 확인'))

    async def test_max_pages_is_partial_and_stated_up_front(self):
        Backend.printed = [['1. Alpha'], ['2. Beta']]
        value = (await self.review(max_pages=1)).structured_content['result']
        self.assertEqual(value['review_status'], 'partial')
        self.assertIsNone(value['numbering_all_match'])
        self.assertEqual([row['status'] for row in value['numbering']], ['match', 'outside_rendered_pages'])
        self.assertIn('일부', value['summary_text'].splitlines()[0])
        self.assertNotIn('기대와 다른', value['summary_text'])

    async def test_an_unavailable_structure_still_returns_previews_and_says_unverified(self):
        Backend.readback_mode = 'unavailable'
        result = await self.review()
        self.assertFalse(result.is_error, result.structured_content)
        value = result.structured_content['result']
        self.assertEqual(value['review_status'], 'unverified')
        self.assertEqual(len(value['pages']), 2)
        self.assertTrue(Path(value['summary_path']).is_file())


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class ReviewWithoutPdfText(Base):
    @classmethod
    def env_for(cls, root):
        # A renderer directory holding pdftoppm only, so the real pdftotext lookup finds nothing.
        tools = root / 'tools'
        tools.mkdir()
        shutil.copy2(shutil.which('pdftoppm'), tools / 'pdftoppm')
        return {'HWP_PDFTOPPM': str(tools / 'pdftoppm')}

    async def test_missing_text_tool_is_unverified_with_a_null_page_number_result(self):
        result = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        self.assertFalse(result.is_error, result.structured_content)
        value = result.structured_content['result']
        self.assertEqual(value['review_status'], 'unverified')
        self.assertIsNone(value['numbering_all_match'])
        self.assertEqual({row['status'] for row in value['numbering']}, {'unverified'})
        self.assertNotIn('기대와 다른', value['summary_text'])


class HostilePackageOnOpen(Base):
    def write(self, name, data):
        path = self.root / 'input' / name
        path.write_bytes(data)
        return str(path)

    async def assert_opened(self, path):
        result = await self.call('hwpx_open', {'request': {'source_path': path}})
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(result.structured_content['session_id'], SID)
        record = json.loads((self.root / 'artifacts' / SID / 'source.json').read_text(encoding='utf-8'))
        self.assertIsNone(record['package'])
        self.assertIn(record['package_error'], {'package_corrupt', 'package_limit'})

    async def test_corrupt_member_does_not_lose_the_session(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('Contents/section0.xml', b'<a/>' * 2000)
        raw = bytearray(out.getvalue())
        start = raw.find(b'section0.xml') + len('section0.xml')
        raw[start:start + 30] = b'\xff' * 30
        await self.assert_opened(self.write('corrupt.hwpx', bytes(raw)))

    async def test_large_decompressed_section_does_not_lose_the_session(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('Contents/section0.xml', b'\0' * (65 * 1024 * 1024))
        await self.assert_opened(self.write('bomb.hwpx', out.getvalue()))


if __name__ == '__main__':
    unittest.main()
