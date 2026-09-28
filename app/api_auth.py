"""Optional bearer-token authentication for the HTTP API.

When ``HWP_API_TOKEN`` is empty the API behaves as before and relies on the
loopback bind for isolation. When it is set, every request must carry
``Authorization: Bearer <token>``. The one exception is an unauthenticated
``GET /health``, which receives a minimal liveness payload (status and port
only) so installer health probes keep working without exposing paths or queue
state.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def is_loopback_host(host: str) -> bool:
    return host.strip().strip('[]').lower() in LOOPBACK_HOSTS


def _authorization_header(scope: Scope) -> bytes | None:
    values = [value for name, value in scope.get('headers') or () if name.lower() == b'authorization']
    if len(values) != 1:
        return None
    return values[0]


def token_matches(header: bytes | None, token: str) -> bool:
    if not token or header is None:
        return False
    return secrets.compare_digest(header, ('Bearer ' + token).encode('ascii'))


async def _send_json(send: Send, status: int, payload: dict[str, Any], extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode('utf-8')
    headers = [
        (b'content-type', b'application/json'),
        (b'content-length', str(len(body)).encode('ascii')),
        (b'cache-control', b'no-store'),
    ]
    headers.extend(extra_headers or [])
    await send({'type': 'http.response.start', 'status': status, 'headers': headers})
    await send({'type': 'http.response.body', 'body': body})


class ApiTokenMiddleware:
    """Pure ASGI middleware so streaming responses pass through unbuffered."""

    def __init__(self, app: ASGIApp, *, token: str, api_port: int) -> None:
        self.app = app
        self.token = token
        self.api_port = api_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type') not in {'http', 'websocket'} or not self.token:
            await self.app(scope, receive, send)
            return
        if token_matches(_authorization_header(scope), self.token):
            await self.app(scope, receive, send)
            return
        if scope.get('type') == 'http' and scope.get('method') == 'GET' and scope.get('path') == '/health':
            await _send_json(send, 200, {'ok': True, 'status': 'ok', 'api_port': self.api_port})
            return
        if scope.get('type') == 'websocket':
            await receive()
            await send({'type': 'websocket.close', 'code': 1008})
            return
        await _send_json(
            send,
            401,
            {'detail': 'Missing or invalid API token.'},
            [(b'www-authenticate', b'Bearer')],
        )
