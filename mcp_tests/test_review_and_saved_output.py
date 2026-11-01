"""Socket-level adapter contract with the pinned SDK, a fake REST backend and the real local Poppler. Not Hancom evidence."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
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
SID = 'c' * 32


def pdf_bytes(pages: list[list[str]]) -> bytes:
    """A small real PDF so pdftoppm and pdftotext run for real."""
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


def hwpx_bytes(payload: bytes = b'image') -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('Contents/section0.xml', '<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p><hp:tbl/><hp:pic/></hp:p></hs:sec>')
        archive.writestr('BinData/image1.png', payload)
    return out.getvalue()


def paragraph(text, heading='OUTLINE', level=0):
    return {'text': text, 'heading': heading, 'level': level, 'in_body': True, 'content_sha256': 'x' + text}


class Backend(BaseHTTPRequestHandler):
    calls: list = []
    paragraphs = [paragraph('Alpha'), paragraph('Beta', level=1)]
    printed = ['1. Alpha', 'X. Beta']
    working_copy_status = 200

    def log_message(self, format, *args):
        pass

    def reply(self, payload, status=200):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/octet-stream' if isinstance(payload, bytes) else 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        type(self).calls.append(('GET', self.path))
        if self.path.startswith('/interactive/session/status'):
            return self.reply({'ok': True, 'session': {'session_id': SID, 'state': 'ready', 'metadata': {'local_cli_v1': {'opened_via': 'local_cli_v1'}}}})
        if self.path.endswith('/artifact/export'):
            return self.reply(pdf_bytes([type(self).printed, ['second page']]))
        if self.path.endswith('/artifact/working-copy'):
            return self.reply(hwpx_bytes(), type(self).working_copy_status)
        return self.reply({'ok': True})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        body = json.loads(raw) if self.headers.get('Content-Type', '').startswith('application/json') else None
        type(self).calls.append(('POST', self.path, body))
        if self.path == '/local-cli/open':
            return self.reply({'ok': True, 'session_id': SID, 'working_copy_id': SID})
        if self.path == '/local-cli/numbering-readback':
            return self.reply({'ok': True, 'session_id': SID, 'source_filename': 'fixture.hwpx', 'page_count': 2, 'document_is_modified': True,
                               'paragraphs': type(self).paragraphs, 'controls': {'tbl': 1}, 'page_numbers': [], 'page_restarts': [], 'caret_unchanged': True,
                               'evidence': {'header_footer': [], 'styles_sha256': 's', 'media_sha256': 'm', 'numberings_sha256': 'n', 'bindata_sha256': 'b'}})
        if self.path == '/local-cli/export':
            return self.reply({'ok': True, 'session_id': SID, 'download_path': f'/local-cli/session/{SID}/artifact/export'})
        if self.path == '/local-cli/save':
            return self.reply({'ok': True, 'session_id': SID, 'filename': 'fixture-edited.hwpx', 'download_path': f'/local-cli/session/{SID}/artifact/working-copy'})
        if self.path in {'/local-cli/undo', '/local-cli/redo'}:
            return self.reply({'ok': True, 'summary': self.path.rsplit('/', 1)[1] + ' applied'})
        if self.path == '/local-cli/command-bundle':
            return self.reply({'ok': True, 'semantic_ok': True, 'steps': body['steps']})
        return self.reply({'ok': False}, 404)


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class ReviewAndSavedOutput(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / 'input').mkdir()
        cls.source = cls.root / 'input' / 'fixture.hwpx'
        cls.source.write_bytes(hwpx_bytes())
        cls.outside = cls.root / 'outside.hwpx'
        cls.outside.write_bytes(hwpx_bytes())
        cls.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{cls.port}/mcp'
        cls.token = secrets.token_urlsafe(32)
        env = os.environ.copy()
        env.update(HWPX_MCP_TOKEN=cls.token, HWPX_MCP_PORT=str(cls.port), HWPX_MCP_BACKEND=f'http://127.0.0.1:{cls.backend.server_port}',
                   HWPX_MCP_SOURCE_ROOT=str(cls.root / 'input'), HWPX_MCP_ARTIFACT_ROOT=str(cls.root / 'artifacts'), HWPX_MCP_TIMEOUT='20')
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
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.log.close()
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        Backend.calls = []
        Backend.paragraphs = [paragraph('Alpha'), paragraph('Beta', level=1)]
        Backend.printed = ['1. Alpha', 'X. Beta']
        Backend.working_copy_status = 200
        shutil.rmtree(self.root / 'artifacts', ignore_errors=True)

    async def call(self, name, args):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                return await client.call_tool(name, args)

    async def review(self, **request):
        return await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', **request}})

    async def test_review_reports_what_was_printed_with_previews_and_files(self):
        result = await self.review(inline_images=1)
        self.assertFalse(result.is_error, result.structured_content)
        value = result.structured_content['result']
        self.assertEqual([row['rendered_label'] for row in value['numbering']], ['1.', 'X.'])
        self.assertFalse(value['numbering_all_match'])
        self.assertIn('번호가 기대와 다른 문단 1개', value['summary_text'])
        self.assertEqual(value['pages_rendered'], 2)
        self.assertEqual([type(item).__name__ for item in result.content], ['TextContent', 'TextContent', 'ImageContent'])
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertEqual(result.content[1].text, value['summary_text'])
        for path in (value['summary_path'], value['json_path'], value['pdf']['path'], value['pages'][1]['path']):
            self.assertTrue(Path(path).is_file(), path)
        self.assertIn('번호 문단', Path(value['summary_path']).read_text(encoding='utf-8'))
        self.assertNotIn('paragraphs', value)
        self.assertEqual([call[1] for call in Backend.calls if call[0] == 'POST'], ['/local-cli/numbering-readback', '/local-cli/export'])

    async def test_a_second_review_compares_with_the_first(self):
        first = (await self.review()).structured_content['result']
        self.assertIsNone(first['comparison'])
        Backend.paragraphs = [paragraph('Alpha'), paragraph('New'), paragraph('Beta', level=1)]
        Backend.printed = ['1. Alpha', '2. New', 'Y. Beta']
        second = (await self.review()).structured_content['result']
        self.assertEqual(second['comparison']['baseline'], first['review_id'])
        self.assertEqual([(c['kind'], c['text']) for c in second['comparison']['changes']], [('added', 'New'), ('number_changed', 'Beta')])
        third = (await self.review(compare_to=second['review_id'])).structured_content['result']
        self.assertEqual(third['comparison']['changes'], [])

    async def test_compare_to_is_bound_to_an_existing_review_under_the_root(self):
        result = await self.review(compare_to='d' * 32 + '/review-1')
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['code'], 'REVIEW_NOT_FOUND')
        result = await self.review(compare_to='../../secret/review-1')
        self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')

    async def test_save_copies_the_file_out_and_compares_it_with_the_source(self):
        await self.call('hwpx_open', {'request': {'source_path': str(self.source)}})
        result = await self.call('hwpx_save', {'session_id': SID})
        saved = result.structured_content['result']['saved_output']
        self.assertTrue(saved['retrieved'])
        self.assertEqual(Path(saved['path']).read_bytes(), hwpx_bytes())
        self.assertTrue(saved['compared_with_source']['bindata_identical'])
        again = (await self.call('hwpx_save', {'session_id': SID})).structured_content['result']['saved_output']
        self.assertNotEqual(again['path'], saved['path'])
        reviewed = (await self.review()).structured_content['result']
        self.assertEqual(reviewed['saved_output']['path'], again['path'])

    async def test_a_failed_copy_does_not_turn_a_native_save_into_a_failure(self):
        Backend.working_copy_status = 500
        result = await self.call('hwpx_save', {'session_id': SID})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content['result']['saved_output'], {
            'retrieved': False, 'code': 'ARTIFACT_DOWNLOAD_FAILED', 'message': 'Saved document could not be retrieved.'})

    async def test_a_saved_output_can_be_reopened_but_other_paths_cannot(self):
        saved = (await self.call('hwpx_save', {'session_id': SID})).structured_content['result']['saved_output']
        opened = await self.call('hwpx_open', {'request': {'source_path': saved['path']}})
        self.assertFalse(opened.is_error, opened.structured_content)
        refused = await self.call('hwpx_open', {'request': {'source_path': str(self.outside)}})
        self.assertEqual(refused.structured_content['error']['code'], 'SOURCE_NOT_ALLOWED')

    async def test_undo_redo_and_paragraph_edits_reach_their_existing_routes(self):
        for op in ('undo', 'redo'):
            result = await self.call('hwpx_command', {'session_id': SID, 'request': {'op': op}})
            self.assertFalse(result.is_error, result.structured_content)
            self.assertEqual(Backend.calls[-1], ('POST', f'/local-cli/{op}', {'session_id': SID}))
        await self.call('hwpx_command', {'session_id': SID, 'request': {'op': 'paragraph_insert_exact', 'match': 'Item', 'expected_page': 1, 'text': 'New', 'confirm_layout': True}})
        step = Backend.calls[-1][2]['steps'][0]
        self.assertEqual((step['op'], step['match'], step['text'], step['position'], step['confirm_layout']), ('paragraph_insert_exact', 'Item', 'New', 'after', True))
        await self.call('hwpx_command', {'session_id': SID, 'request': {'op': 'paragraph_remove_exact', 'match': 'Item', 'expected_page': 1, 'confirm_remove': True}})
        self.assertEqual(Backend.calls[-1][2]['steps'][0]['op'], 'paragraph_remove_exact')

    async def test_a_refusal_leads_with_its_reason(self):
        from hwpx_mcp.server import rejection_summary
        refused = {'ok': False, 'steps': [{'ok': False, 'refusal_code': 'target_has_controls', 'mutation_may_have_persisted': False}]}
        self.assertEqual(rejection_summary(refused), {'reason': 'target_has_controls', 'stopped_before_edit': True, 'session_held': False})
        held = {'ok': False, 'native_custody_held': True, 'steps': [{'ok': False, 'mutation_may_have_persisted': True,
                                                                     'failure_diagnostic': {'numbering_reason': 'native_failed'}}]}
        self.assertEqual(rejection_summary(held), {'reason': 'native_failed', 'stopped_before_edit': False, 'session_held': True})
        self.assertEqual(rejection_summary({'detail': 'blocked'}), {'reason': 'blocked', 'session_held': False})

    async def test_paragraph_edit_schema_is_closed(self):
        for request in (
            {'op': 'paragraph_remove_exact', 'match': 'Item', 'expected_page': 1, 'confirm_remove': False},
            {'op': 'paragraph_remove_exact', 'match': 'Item', 'expected_page': 1},
            {'op': 'paragraph_insert_exact', 'match': 'Item', 'expected_page': 1, 'text': 'New'},
            {'op': 'paragraph_insert_exact', 'match': 'Item', 'expected_page': 1, 'text': 'New', 'confirm_layout': True, 'position': 'inside'},
            {'op': 'paragraph_insert_exact', 'match': 'a\nb', 'expected_page': 1, 'text': 'New', 'confirm_layout': True},
            {'op': 'undo', 'count': 2},
        ):
            with self.subTest(request=request):
                calls = len(Backend.calls)
                result = await self.call('hwpx_command', {'session_id': SID, 'request': request})
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual(len(Backend.calls), calls)


if __name__ == '__main__':
    unittest.main()
