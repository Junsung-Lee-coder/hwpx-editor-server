from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from app.api_auth import ApiTokenMiddleware, is_loopback_host
from app.config import Settings
from local_cli_v1 import transport

TOKEN = 'a' * 40


async def _downstream(scope, receive, send):
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'{"full": true}'})


def _call(middleware, *, path='/local-cli/status', method='GET', headers=()):
    scope = {'type': 'http', 'method': method, 'path': path, 'headers': list(headers)}
    messages = []

    async def receive():
        return {'type': 'http.request', 'body': b''}

    async def send(message):
        messages.append(message)

    asyncio.run(middleware(scope, receive, send))
    status = messages[0]['status']
    body = b''.join(m.get('body', b'') for m in messages[1:])
    return status, json.loads(body)


class ApiTokenMiddlewareTests(unittest.TestCase):
    def test_empty_token_passes_every_request_through(self) -> None:
        status, body = _call(ApiTokenMiddleware(_downstream, token='', api_port=8765))
        self.assertEqual((status, body), (200, {'full': True}))

    def test_missing_or_wrong_token_is_rejected(self) -> None:
        middleware = ApiTokenMiddleware(_downstream, token=TOKEN, api_port=8765)
        for headers in ((), [(b'authorization', b'Bearer wrong')], [(b'authorization', TOKEN.encode())]):
            with self.subTest(headers=headers):
                status, body = _call(middleware, headers=headers)
                self.assertEqual(status, 401)
                self.assertEqual(body, {'detail': 'Missing or invalid API token.'})

    def test_duplicate_authorization_headers_are_rejected(self) -> None:
        middleware = ApiTokenMiddleware(_downstream, token=TOKEN, api_port=8765)
        header = (b'authorization', f'Bearer {TOKEN}'.encode())
        status, _ = _call(middleware, headers=[header, header])
        self.assertEqual(status, 401)

    def test_valid_token_reaches_the_app(self) -> None:
        middleware = ApiTokenMiddleware(_downstream, token=TOKEN, api_port=8765)
        status, body = _call(middleware, headers=[(b'Authorization', f'Bearer {TOKEN}'.encode())])
        self.assertEqual((status, body), (200, {'full': True}))

    def test_unauthenticated_health_gets_minimal_liveness_only(self) -> None:
        middleware = ApiTokenMiddleware(_downstream, token=TOKEN, api_port=18765)
        status, body = _call(middleware, path='/health')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True, 'status': 'ok', 'api_port': 18765})

    def test_unauthenticated_non_get_health_is_rejected(self) -> None:
        middleware = ApiTokenMiddleware(_downstream, token=TOKEN, api_port=8765)
        status, _ = _call(middleware, path='/health', method='POST')
        self.assertEqual(status, 401)


class ApiTokenSettingsTests(unittest.TestCase):
    def test_loopback_hosts(self) -> None:
        for host in ('127.0.0.1', 'localhost', '::1', '[::1]'):
            self.assertTrue(is_loopback_host(host), host)
        for host in ('0.0.0.0', '192.168.0.10', '::'):
            self.assertFalse(is_loopback_host(host), host)

    def test_non_loopback_host_requires_token(self) -> None:
        with patch.dict(os.environ, {'HWP_API_HOST': '0.0.0.0'}, clear=True):
            with self.assertRaises(ValueError):
                Settings(_env_file=None)
        with patch.dict(os.environ, {'HWP_API_HOST': '0.0.0.0', 'HWP_API_TOKEN': TOKEN}, clear=True):
            self.assertEqual(Settings(_env_file=None).api_token, TOKEN)

    def test_default_loopback_needs_no_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings(_env_file=None).api_token, '')

    def test_weak_token_is_rejected(self) -> None:
        for token in ('short', 'a' * 31, 'a' * 20 + ' ' + 'a' * 20):
            with self.subTest(token=token), patch.dict(os.environ, {'HWP_API_TOKEN': token}, clear=True):
                with self.assertRaises(ValueError):
                    Settings(_env_file=None)

    def test_token_is_not_in_settings_repr(self) -> None:
        with patch.dict(os.environ, {'HWP_API_TOKEN': TOKEN}, clear=True):
            self.assertNotIn(TOKEN, repr(Settings(_env_file=None)))


class CliTransportTokenTests(unittest.TestCase):
    def test_token_is_sent_only_to_the_configured_origin(self) -> None:
        base = 'http://127.0.0.1:8765'
        with patch.dict(os.environ, {'HWPX_API_TOKEN': TOKEN}):
            self.assertEqual(
                transport._auth_headers(base, f'{base}/local-cli/status'),
                {'Authorization': f'Bearer {TOKEN}'},
            )
            self.assertEqual(transport._auth_headers(base, 'http://127.0.0.1:9999/x'), {})
            self.assertEqual(transport._auth_headers(base, 'https://example.com/x'), {})

    def test_no_token_sends_no_header(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(transport._auth_headers('http://127.0.0.1:8765', 'http://127.0.0.1:8765/x'), {})

    def test_get_json_attaches_header(self) -> None:
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{}'

        def fake_raw(method, url, *, headers=None, body=None):
            captured['headers'] = headers
            return _Response()

        with patch.dict(os.environ, {'HWPX_API_TOKEN': TOKEN}), patch.object(transport, '_request_raw', fake_raw):
            transport.get_json('http://127.0.0.1:8765', '/local-cli/status')
        self.assertEqual(captured['headers'], {'Authorization': f'Bearer {TOKEN}'})


if __name__ == '__main__':
    unittest.main()
