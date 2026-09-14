from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote_from_bytes

from fastapi import FastAPI
from app.local_cli_router import build_local_cli_router
from app.local_cli_service import LocalCliService
from starlette.responses import StreamingResponse


ROOT = Path(__file__).resolve().parents[1]


async def _consume_response(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    iterator = response.body_iterator
    if hasattr(iterator, '__aiter__'):
        async for chunk in iterator:
            chunks.append(bytes(chunk))
    else:
        for chunk in iterator:
            chunks.append(bytes(chunk))
    background = response.background
    if background is not None:
        await background()
    return b''.join(chunks)


async def _invoke_asgi(app: FastAPI, path: str) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.sleep(3600)
        return {'type': 'http.disconnect'}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    raw_path = path.encode('ascii')
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.0'},
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': path,
        'raw_path': raw_path,
        'query_string': b'',
        'root_path': '',
        'headers': [],
        'client': ('test-client', 1),
        'server': ('test-server', 80),
        'state': {},
    }
    await app(scope, receive, send)
    return messages


def _session_record(session_id: str, source_path: Path, *, state: str = 'open', metadata: object = None) -> dict[str, object]:
    return {
        'session_id': session_id,
        'state': state,
        'source_path': str(source_path),
        'source_filename': source_path.name,
        'metadata': metadata if metadata is not None else {'local_cli_v1': {'opened_via': 'local_cli_v1'}},
        'created_at': '2026-01-01T00:00:00+00:00',
        'updated_at': '2026-01-01T00:00:00+00:00',
    }


def _make_bound_service(root: Path, *, session_id: str = 's1', recovery: bool = False) -> tuple[LocalCliService, dict[str, object], Path, Path | None]:
    service = object.__new__(LocalCliService)
    service.root = root / 'local_cli_v1'
    service.sessions_root = service.root / 'sessions'
    service.active_binding_path = service.root / 'active_binding.json'
    service.root.mkdir(parents=True)
    service.sessions_root.mkdir(parents=True)
    session_root = service.sessions_root / session_id
    working_copy = session_root / 'working' / 'working-copy.hwpx'
    working_copy.parent.mkdir(parents=True)
    working_bytes = b'working-copy bytes'
    working_copy.write_bytes(working_bytes)
    artifacts: dict[str, object] = {'latest_working_copy_path': str(working_copy)}
    custody: dict[str, object] = {
        'working-copy': {
            'size_bytes': len(working_bytes),
            'sha256': hashlib.sha256(working_bytes).hexdigest(),
        }
    }
    recovery_path: Path | None = None
    if recovery:
        recovery_path = session_root / 'recovery' / 'recovered.hwpx'
        recovery_path.parent.mkdir(parents=True)
        recovery_bytes = b'recovery bytes'
        recovery_path.write_bytes(recovery_bytes)
        artifacts['latest_recovery_path'] = str(recovery_path)
        custody['recovery'] = {
            'size_bytes': len(recovery_bytes),
            'sha256': hashlib.sha256(recovery_bytes).hexdigest(),
        }
    binding: dict[str, object] = {
        'session_id': session_id,
        'session_root_path': str(session_root),
        'session_root_identity': service._managed_path_identity(session_root),
        'source_filename': 'working-copy.hwpx',
        'working_copy_path': str(working_copy),
        'document_session_state': 'reconciled' if recovery else 'open',
        'live_session_bound': not recovery,
        'artifacts': artifacts,
        'artifact_custody': custody,
    }
    binding_path = service._binding_path(session_id)
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(json.dumps(binding), encoding='utf-8')
    service.active_binding_path.write_text(json.dumps(binding), encoding='utf-8')
    return service, binding, working_copy, recovery_path


class R22ArtifactProjectionTests(unittest.TestCase):
    def test_forged_origin_without_binding_does_not_advertise_route(self) -> None:
        from app.api_server import _public_interactive_session

        with tempfile.TemporaryDirectory() as raw:
            service = object.__new__(LocalCliService)
            service.root = Path(raw) / 'local_cli_v1'
            service.sessions_root = service.root / 'sessions'
            service.active_binding_path = service.root / 'active_binding.json'
            record = _session_record('f' * 32, Path(raw) / 'private.hwpx')
            record['artifacts'] = {
                'latest_working_copy_download_path': '/local-cli/session/forged/artifact/working-copy',
                'latest_recovery_sha256': 'forged',
            }
            with patch('app.api_server.local_cli_service', service):
                public = _public_interactive_session(record)

        self.assertEqual(public['source_path'], '<redacted>')
        self.assertNotIn('download_path', json.dumps(public))

    def test_mismatched_binding_and_missing_custody_are_fail_closed(self) -> None:
        from app.api_server import _public_interactive_session

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, binding, working_copy, _ = _make_bound_service(root)
            mismatched = dict(binding)
            mismatched['session_id'] = 'other'
            service._binding_path('s1').write_text(json.dumps(mismatched), encoding='utf-8')
            record = _session_record('s1', working_copy)
            with patch('app.api_server.local_cli_service', service):
                public = _public_interactive_session(record)
            self.assertNotIn('download_path', json.dumps(public))

            service, _binding, working_copy, _ = _make_bound_service(root / 'missing-custody')
            binding_path = service._binding_path('s1')
            changed = json.loads(binding_path.read_text(encoding='utf-8'))
            changed['artifact_custody'] = {}
            binding_path.write_text(json.dumps(changed), encoding='utf-8')
            service.active_binding_path.write_text(json.dumps(changed), encoding='utf-8')
            with patch('app.api_server.local_cli_service', service):
                public = _public_interactive_session(_session_record('s1', working_copy))
            self.assertNotIn('download_path', json.dumps(public))

    def test_closed_cleanup_binding_does_not_advertise_route(self) -> None:
        from app.api_server import _public_interactive_session

        with tempfile.TemporaryDirectory() as raw:
            service, binding, working_copy, _ = _make_bound_service(Path(raw))
            binding['document_session_state'] = 'closed_cleanup_pending'
            service._binding_path('s1').write_text(json.dumps(binding), encoding='utf-8')
            service.active_binding_path.write_text(json.dumps(binding), encoding='utf-8')
            with patch('app.api_server.local_cli_service', service):
                public = _public_interactive_session(_session_record('s1', working_copy))
        self.assertNotIn('download_path', json.dumps(public))

    def test_authoritative_working_copy_and_recovery_custody_project_routes(self) -> None:
        from app.api_server import _public_interactive_session

        with tempfile.TemporaryDirectory() as raw:
            service, _binding, working_copy, recovery_path = _make_bound_service(Path(raw), recovery=True)
            record = _session_record('s1', working_copy, state='failed')
            with patch('app.api_server.local_cli_service', service):
                public = _public_interactive_session(record)
            serialized = json.dumps(public)

        self.assertNotIn(str(Path(raw)), serialized)
        self.assertEqual(public['source_path'], '/local-cli/session/s1/artifact/working-copy')
        self.assertEqual(
            public['artifacts']['latest_working_copy_download_path'],
            '/local-cli/session/s1/artifact/working-copy',
        )
        self.assertEqual(
            public['artifacts']['latest_recovery_download_path'],
            '/local-cli/session/s1/artifact/recovery',
        )
        self.assertTrue(recovery_path is not None)


class R22UnicodeDownloadTests(unittest.TestCase):
    def _router(self, download: object):
        service = SimpleNamespace(open_artifact=lambda **_kwargs: download)
        return build_local_cli_router(settings=SimpleNamespace(spool_root=Path(tempfile.gettempdir())), interactive_sessions=object(), service=service)

    def test_asgi_stream_has_ascii_fallback_encoded_filename_and_closes(self) -> None:
        download = SimpleNamespace(
            stream=io.BytesIO(b'correct Korean artifact bytes'),
            filename='한글문서-edited.hwpx',
            close=lambda: download.stream.close(),
        )
        router = self._router(download)
        route = next(route for route in router.routes if route.path.endswith('/artifact/{kind}'))
        response = route.endpoint('s1', 'working-copy')
        self.assertIsInstance(response, StreamingResponse)
        disposition = response.headers['content-disposition']
        self.assertIn('filename="', disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertNotRegex(disposition, r'[\u0080-\uffff]')
        encoded = quote_from_bytes(download.filename.encode('utf-8'), safe="!#$&+-.^_`|~")
        self.assertIn(f"filename*=UTF-8''{encoded}", disposition)
        self.assertEqual(asyncio.run(_consume_response(response)), b'correct Korean artifact bytes')
        self.assertTrue(download.stream.closed)

    def test_asgi_application_delivers_unicode_artifact_and_closes(self) -> None:
        stream = io.BytesIO(b'ASGI application bytes')
        download = SimpleNamespace(
            stream=stream,
            filename='한글문서.hwpx',
            size_bytes=len(b'ASGI application bytes'),
            close=stream.close,
        )
        app = FastAPI()
        app.include_router(self._router(download))
        messages = asyncio.run(_invoke_asgi(app, '/local-cli/session/s1/artifact/working-copy'))
        start = next(message for message in messages if message['type'] == 'http.response.start')
        body = b''.join(
            bytes(message.get('body', b''))
            for message in messages
            if message['type'] == 'http.response.body'
        )
        headers = dict(start['headers'])
        disposition = bytes(headers[b'content-disposition']).decode('latin-1')
        self.assertEqual(start['status'], 200)
        self.assertEqual(body, b'ASGI application bytes')
        self.assertIn('filename="', disposition)
        self.assertIn("filename*=UTF-8''%ED%95%9C%EA%B8%80%EB%AC%B8%EC%84%9C.hwpx", disposition)
        self.assertNotRegex(disposition, r'[\u0080-\uffff]')
        self.assertTrue(stream.closed)

    def test_stream_closes_when_response_construction_fails(self) -> None:
        stream = io.BytesIO(b'bytes')
        closed = []
        download = SimpleNamespace(stream=stream, filename='한글.hwpx', close=lambda: (closed.append(True), stream.close()))
        router = self._router(download)
        route = next(route for route in router.routes if route.path.endswith('/artifact/{kind}'))
        with patch('app.local_cli_router.StreamingResponse', side_effect=RuntimeError('construction failed')):
            with self.assertRaisesRegex(RuntimeError, 'construction failed'):
                route.endpoint('s1', 'working-copy')
        self.assertTrue(stream.closed)
        self.assertEqual(closed, [True])


class R22SourceHygieneTests(unittest.TestCase):
    def test_current_shipped_python_sources_have_no_unfinished_stage_wording(self) -> None:
        from scripts.source_bundle_policy import find_python_source_hygiene_violations

        self.assertEqual(find_python_source_hygiene_violations(ROOT), [])

    def test_source_hygiene_checks_every_python_member(self) -> None:
        from scripts.source_bundle_policy import UNFINISHED_PYTHON_SOURCE_PHRASES, find_python_source_hygiene_violations

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'app').mkdir()
            marker = UNFINISHED_PYTHON_SOURCE_PHRASES[0]
            (root / 'app' / 'stale.py').write_text(f'"""{marker}"""\n', encoding='utf-8')
            violations = find_python_source_hygiene_violations(root)

        self.assertEqual(len(violations), 1)
        self.assertIn('app/stale.py', violations[0])


if __name__ == '__main__':
    unittest.main()
