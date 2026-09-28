from __future__ import annotations

import http.client
import json
import mimetypes
import os
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request

DEFAULT_BASE_URL = os.environ.get('HWPX_BASE_URL', 'http://127.0.0.1:8765').rstrip('/')


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _decode_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict):
        raise ApiError('Server returned a non-object response.')
    return payload


_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = parse.urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or '').lower(), parts.port


def _auth_headers(base_url: str, url: str) -> dict[str, str]:
    """Attach HWPX_API_TOKEN only to requests aimed at the configured server.

    The token is never sent in cleartext off the machine: a plain-HTTP
    request to a non-loopback host is refused while a token is configured.
    """
    token = os.environ.get('HWPX_API_TOKEN', '')
    if not token or _origin(url) != _origin(base_url):
        return {}
    scheme, host, _port = _origin(url)
    if scheme != 'https' and host not in _LOOPBACK_HOSTS:
        raise ApiError(
            f'Refusing to send HWPX_API_TOKEN over {scheme or "unknown"}:// to non-loopback host {host!r}; '
            'use an https:// URL (for example through a TLS proxy or tunnel).'
        )
    return {'Authorization': f'Bearer {token}'}


class _RefuseCredentialedRedirect(request.HTTPRedirectHandler):
    """Do not follow redirects for requests that carry the API token.

    urllib copies request headers onto the redirected request, so following a
    redirect could forward the bearer token to another origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header('Authorization'):
            fp.close()
            raise ApiError(
                f'Refusing to follow HTTP {code} redirect for a request that carries the API token.',
                status_code=code,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener_for(req: request.Request) -> request.OpenerDirector:
    """Build the opener per request so environment proxies are read at call time.

    A request that carries the API token never goes through a proxy: the
    loopback/https checks in _auth_headers cover the destination, and a
    proxy would receive the bearer header on a route they do not see.
    """
    if req.has_header('Authorization'):
        return request.build_opener(request.ProxyHandler({}), _RefuseCredentialedRedirect)
    return request.build_opener(_RefuseCredentialedRedirect)


def _request_raw(method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None):
    req = request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        return _opener_for(req).open(req)
    except error.HTTPError as exc:
        payload = {}
        try:
            payload = _decode_json(exc.read())
        except (OSError, ValueError, http.client.HTTPException, ApiError):
            payload = {}
        detail = payload.get('detail') or payload.get('error') or str(exc)
        raise ApiError(str(detail), status_code=exc.code) from exc
    except error.URLError as exc:
        raise ApiError(f'Failed to reach HWPX server: {exc.reason}') from exc


def _request(
    method: str,
    base_url: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> dict[str, Any]:
    merged = {**(headers or {}), **_auth_headers(base_url, url)}
    with _request_raw(method, url, headers=merged, body=body) as response:
        return _decode_json(response.read())


def get_json(base_url: str, path: str) -> dict[str, Any]:
    return _request('GET', base_url, f'{base_url}{path}')


def post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return _request(
        'POST',
        base_url,
        f'{base_url}{path}',
        headers={'Content-Type': 'application/json; charset=utf-8'},
        body=body,
    )


def download_to_path(base_url: str, path: str, destination: Path) -> Path:
    if path.startswith('file://'):
        source = Path(path[7:])
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.resolve() == destination.resolve():
                return destination
        except FileNotFoundError:
            pass
        shutil.copyfile(source, destination)
        return destination
    if path.startswith('/') and Path(path).exists():
        source = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.resolve() == destination.resolve():
                return destination
        except FileNotFoundError:
            pass
        shutil.copyfile(source, destination)
        return destination
    url = path if path.startswith('http://') or path.startswith('https://') else f'{base_url}{path}'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _request_raw('GET', url, headers=_auth_headers(base_url, url)) as response:
        destination.write_bytes(response.read())
    return destination


def post_file(base_url: str, path: str, *, field_name: str, file_path: Path, extra_fields: dict[str, str] | None = None) -> dict[str, Any]:
    boundary = f'----hwpx-local-cli-{uuid.uuid4().hex}'
    parts: list[bytes] = []
    extra_fields = extra_fields or {}
    for key, value in extra_fields.items():
        parts.extend(
            [
                f'--{boundary}\r\n'.encode('utf-8'),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode('utf-8'),
                str(value).encode('utf-8'),
                b'\r\n',
            ]
        )

    content_type = mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream'
    parts.extend(
        [
            f'--{boundary}\r\n'.encode('utf-8'),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode('utf-8'),
            f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8'),
            file_path.read_bytes(),
            b'\r\n',
            f'--{boundary}--\r\n'.encode('utf-8'),
        ]
    )
    body = b''.join(parts)
    return _request(
        'POST',
        base_url,
        f'{base_url}{path}',
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
        body=body,
    )
