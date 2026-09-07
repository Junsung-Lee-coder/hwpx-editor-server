"""Opt-in, loopback-only MCP facade over the existing HWPX REST service.

No COM, shared CLI cache, installer hooks or background backend process is created here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import urlsplit
import uuid

import httpx2
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import InboundLadderRejection, classify_inbound_request
import mcp.types as types
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from hwpx_mcp.schema import MODELS, Envelope

PROTOCOL_VERSION = '2026-07-28'
MAX_BODY = 1024 * 1024
MAX_BACKEND = 4 * 1024 * 1024
MAX_FILE = 25 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    token: str
    source_root: Path
    artifact_root: Path
    backend: str = 'http://127.0.0.1:8765'
    port: int = 18766
    timeout: float = 120.0
    backend_token: str = ''

    def __post_init__(self):
        parsed = urlsplit(self.backend)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            raise ValueError('Backend must be an HTTP(S) origin without credentials, path or query.')
        if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} and parsed.scheme != 'https':
            raise ValueError('Non-loopback backends require HTTPS.')
        if len(self.token) < 32 or not self.token.isascii() or any(c.isspace() for c in self.token):
            raise ValueError('HWPX_MCP_TOKEN must be a dedicated secret of at least 32 non-whitespace characters.')
        if not 1024 <= self.port <= 65535 or not .1 <= self.timeout <= 600:
            raise ValueError('Port must be 1024..65535 and timeout 0.1..600 seconds.')
        if not self.source_root.is_absolute() or not self.source_root.is_dir():
            raise ValueError('HWPX_MCP_SOURCE_ROOT must be an existing absolute input directory.')
        if not self.artifact_root.is_absolute():
            raise ValueError('HWPX_MCP_ARTIFACT_ROOT must be an absolute output directory.')
        if self.backend_token and secrets.compare_digest(self.token, self.backend_token):
            raise ValueError('Backend credentials must be separate from the MCP token.')

    @classmethod
    def from_env(cls):
        return cls(token=os.environ.get('HWPX_MCP_TOKEN', ''),
                   source_root=Path(os.environ.get('HWPX_MCP_SOURCE_ROOT', '')),
                   artifact_root=Path(os.environ.get('HWPX_MCP_ARTIFACT_ROOT', '')),
                   backend=os.environ.get('HWPX_MCP_BACKEND', 'http://127.0.0.1:8765'),
                   port=int(os.environ.get('HWPX_MCP_PORT', '18766')),
                   timeout=float(os.environ.get('HWPX_MCP_TIMEOUT', '120')),
                   backend_token=os.environ.get('HWPX_MCP_BACKEND_TOKEN', ''))


class BackendFailure(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)


def envelope(operation, *, sid=None, result=None, error=None):
    return Envelope(ok=error is None, operation=operation, session_id=sid,
                    document_id=sid, result=result, error=error).model_dump(mode='json')


class Facade:
    def __init__(self, settings: Settings):
        self.settings = settings
        # The existing local-CLI backend has one active document binding. Do not
        # pretend that MCP requests make it a multi-document COM runtime.
        self.serial = asyncio.Lock()

    async def request(self, http, method, path, **kwargs):
        async with http.stream(method, self.settings.backend.rstrip('/') + path, **kwargs) as response:
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_BACKEND:
                    raise BackendFailure('BACKEND_RESPONSE_LIMIT', 'Backend response exceeded the bounded result limit.')
            try:
                value = json.loads(chunks)
            except (ValueError, RecursionError):
                raise BackendFailure('BACKEND_INVALID_RESPONSE', 'Backend did not return a JSON object.') from None
            if not isinstance(value, dict):
                raise BackendFailure('BACKEND_INVALID_RESPONSE', 'Backend did not return a JSON object.')
            if response.status_code >= 400 or value.get('ok') is not True:
                # Preserve journal identifiers and backend evidence, not the
                # exception/HTTP request (which could contain credentials).
                raise BackendFailure('BACKEND_REJECTED', 'Backend rejected the operation; inspect its evidence before retrying.',
                                     {'http_status': response.status_code, 'backend': value})
            return value

    async def session(self, http, sid):
        value = await self.request(http, 'GET', '/interactive/session/status', params={'session_id': sid})
        record = value.get('session')
        if not isinstance(record, dict) or record.get('session_id') != sid:
            raise BackendFailure('SESSION_IDENTITY_MISMATCH', 'Backend session identity did not match the explicit handle.')
        metadata = record.get('metadata')
        local = metadata.get('local_cli_v1') if isinstance(metadata, dict) else None
        # r16 replaces opened_via with bridge on commands and closed_via on
        # close. Preserve explicit identity across that backend lifecycle.
        if not isinstance(local, dict) or not (
                local.get('opened_via') == 'local_cli_v1' or local.get('bridge') == 'local_cli_v1' or
                (local.get('closed_via') == 'local_cli_v1' and record.get('state') == 'closed')):
            raise BackendFailure('SESSION_NOT_MANAGED', 'Only server-managed local-CLI working-copy sessions are supported.')
        return value

    async def execute(self, name, args):
        sid = getattr(args, 'session_id', None)
        try:
            await asyncio.wait_for(self.serial.acquire(), timeout=self.settings.timeout)
        except TimeoutError:
            return envelope(name, sid=sid, error={'code': 'ADAPTER_BUSY', 'message': 'Another request owns the backend lane; no operation was submitted.', 'details': {}})
        try:
            headers = {'Authorization': 'Bearer ' + self.settings.backend_token} if self.settings.backend_token else {}
            async with httpx2.AsyncClient(timeout=self.settings.timeout, headers=headers, follow_redirects=False,
                                         trust_env=False) as http:
                if name == 'hwpx_health':
                    result = await self.request(http, 'GET', '/health')
                elif name == 'hwpx_open':
                    raw = Path(args.request.source_path)
                    if not raw.is_absolute():
                        raise BackendFailure('SOURCE_NOT_ALLOWED', 'Source must be an absolute path under the configured input root.')
                    source = raw.resolve(strict=True)
                    if (not source.is_relative_to(self.settings.source_root.resolve()) or
                            source.suffix.lower() not in {'.hwpx', '.hwp'} or not source.is_file()):
                        raise BackendFailure('SOURCE_NOT_ALLOWED', 'Source is not an allowed HWP/HWPX input file.')
                    if not 0 < source.stat().st_size <= MAX_FILE:
                        raise BackendFailure('SOURCE_SIZE_LIMIT', 'Source must be nonempty and no larger than 25 MiB.')
                    with source.open('rb') as stream:
                        result = await self.request(http, 'POST', '/local-cli/open',
                            files={'file': (source.name, stream, 'application/octet-stream')},
                            data={'session_label': args.request.session_label} if args.request.session_label else {})
                    sid = result.get('session_id')
                    from hwpx_mcp.schema import Session
                    Session(session_id=sid)
                    if result.get('working_copy_id') != sid:
                        raise BackendFailure('SESSION_IDENTITY_MISMATCH', 'Open did not return its managed working-copy identity.')
                else:
                    record = await self.session(http, sid)
                    if name == 'hwpx_status':
                        # A closed session is still queryable by explicit identity.
                        result = record
                        live = await self.request(http, 'GET', '/local-cli/status')
                        if live.get('session_id') == sid:
                            result = {**record, 'runtime': live}
                    elif name == 'hwpx_command':
                        request = args.request.model_dump(exclude_none=True)
                        if request['op'] == 'command_reconcile':
                            result = await self.request(http, 'POST', '/local-cli/command-reconcile',
                                json={'session_id': sid, 'command_id': request['command_id']})
                        else:
                            # Reuse the strict existing bundle validation, including
                            # each command's native CAS/proof guard. Never widen it.
                            from local_cli_v1.bundles import CommandStep
                            fields = {k: v for k, v in request.items() if k not in {'op', 'label'}}
                            step = CommandStep(op=request['op'], label=request['label'], fields=fields)
                            result = await self.request(http, 'POST', '/local-cli/command-bundle',
                                json={'session_id': sid, 'steps': [step.to_server_json()]})
                    elif name == 'hwpx_proof':
                        if args.request.kind == 'frame':
                            result = await self.request(http, 'POST', '/local-cli/screenshot', json={'session_id': sid})
                        else:
                            result = await self.page_proof(http, sid, args.request.page, args.request.dpi)
                    else:
                        payload = {'session_id': sid}
                        if name == 'hwpx_find':
                            payload.update(args.request.model_dump(exclude_none=True))
                        result = await self.request(http, 'POST', '/local-cli/' + name.removeprefix('hwpx_'), json=payload)
                    returned_sid = result.get('session_id')
                    if returned_sid is not None and returned_sid != sid:
                        raise BackendFailure('SESSION_IDENTITY_MISMATCH', 'Backend result identity did not match the requested handle.')
                return envelope(name, sid=sid, result=result)
        except (httpx2.TimeoutException, httpx2.TransportError):
            return envelope(name, sid=sid, error={
                'code': 'BACKEND_OUTCOME_UNKNOWN',
                'message': 'Transport ended before the backend outcome was known. Do not replay; query status and reconcile the backend command before continuing. Cancellation is not rollback.',
                'details': {'session_id': sid, 'backend_may_still_be_running': True}})
        except BackendFailure as exc:
            return envelope(name, sid=sid, error={'code': exc.code, 'message': exc.message, 'details': exc.details})
        except (OSError, ValueError):
            return envelope(name, sid=sid, error={'code': 'LOCAL_INPUT_OR_ARTIFACT_ERROR', 'message': 'Input or artifact processing failed; no automatic replay was attempted.', 'details': {}})
        finally:
            self.serial.release()

    async def page_proof(self, http, sid, page, dpi):
        result = await self.request(http, 'POST', '/local-cli/export', json={'session_id': sid})
        expected = f'/local-cli/session/{sid}/artifact/export'
        if result.get('download_path') != expected:
            raise BackendFailure('ARTIFACT_IDENTITY_MISMATCH', 'Export returned an unexpected artifact route.')
        destination = self.settings.artifact_root / sid / uuid.uuid4().hex
        destination.mkdir(parents=True, exist_ok=False)
        pdf = destination / 'document.pdf'
        png = destination / f'page-{page}.png'
        async with http.stream('GET', self.settings.backend.rstrip('/') + expected) as response:
            if response.status_code != 200:
                raise BackendFailure('ARTIFACT_DOWNLOAD_FAILED', 'Native PDF artifact could not be retrieved.')
            size = 0
            with pdf.open('xb') as stream:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise BackendFailure('ARTIFACT_SIZE_LIMIT', 'Native PDF exceeds 25 MiB; retained partial artifact is not proof.')
                    stream.write(chunk)
        # Reuse the existing Poppler resolver, with an explicit subprocess limit.
        from hwpx_mcp.proof import render_page
        await asyncio.to_thread(render_page, pdf, png, page=page, dpi=dpi)
        from PIL import Image
        with Image.open(png) as image:
            width, height = image.size
            image.verify()
        proof = {'kind': 'page-screenshot', 'session_id': sid, 'page': page, 'dpi': dpi,
                 'path': str(png), 'sha256': hashlib.sha256(png.read_bytes()).hexdigest(),
                 'width': width, 'height': height, 'pdf_path': str(pdf),
                 'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
                 'not_full_document_qa': True}
        (destination / 'manifest.json').write_text(json.dumps(proof, indent=2), encoding='utf-8')
        return {**result, 'page_proof': proof}


class Boundary:
    """Authentication/exact-authority and modern-only routing around the official SDK.

    The SDK owns protocol validation/dispatch/SSE. Its dual-era default would
    otherwise accept legacy traffic that this adapter does not advertise.
    """
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.hosts = {f'127.0.0.1:{settings.port}', f'localhost:{settings.port}'}
        self.origins = {'http://' + host for host in self.hosts}

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if (len(headers.getlist('host')) != 1 or headers.get('host') not in self.hosts or
                len(headers.getlist('origin')) > 1 or
                ('origin' in headers and headers['origin'] not in self.origins)):
            return await JSONResponse({'error': 'Forbidden authority or origin'}, 403)(scope, receive, send)
        auth = headers.getlist('authorization')
        if len(auth) != 1 or not secrets.compare_digest(auth[0].encode('utf-8'), ('Bearer ' + self.settings.token).encode('ascii')):
            return await JSONResponse({'error': 'Authentication required'}, 401,
                headers={'WWW-Authenticate': 'Bearer realm="hwpx-mcp-local"'})(scope, receive, send)
        if scope['path'] != '/mcp':
            return await Response(status_code=404)(scope, receive, send)
        if scope['method'] != 'POST':
            return await Response(status_code=405, headers={'Allow': 'POST'})(scope, receive, send)
        if headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return await JSONResponse({'error': 'Content-Type must be application/json'}, 415)(scope, receive, send)
        # Bound the body before the SDK buffers it; replay only into the same
        # in-memory ASGI request, never to the backend.
        raw = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            raw.extend(message.get('body', b''))
            if len(raw) > MAX_BODY:
                return await JSONResponse({'error': 'Request too large'}, 413)(scope, receive, send)
            if not message.get('more_body', False):
                break
        if headers.get('mcp-protocol-version') != PROTOCOL_VERSION:
            try:
                body = json.loads(raw)
            except (ValueError, RecursionError):
                body = None
            # Modern metadata ladder from the pinned SDK preserves missing vs
            # contradictory vs unsupported version classification.
            if isinstance(body, dict) and 'method' in body and isinstance(body['method'], str):
                verdict = classify_inbound_request(body, headers=dict(headers), supported_modern_versions=[PROTOCOL_VERSION])
                if isinstance(verdict, InboundLadderRejection):
                    error = {'code': verdict.code, 'message': verdict.message}
                    if verdict.data is not None:
                        error['data'] = verdict.data
                    response = {'jsonrpc': '2.0', 'error': error}
                    if 'id' in body:
                        response['id'] = body['id']
                    return await JSONResponse(response, 400)(scope, receive, send)
            # For malformed envelopes let the official modern parser classify
            # syntax before metadata. This routing header does not reach handlers.
            scope = {**scope, 'headers': [(k, v) for k, v in scope['headers'] if k.lower() != b'mcp-protocol-version'] + [(b'mcp-protocol-version', PROTOCOL_VERSION.encode())]}
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(raw), 'more_body': False}
            return await receive()
        await self.app(scope, replay, send)


def build_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    facade = Facade(settings)
    descriptors = []
    for name, model in sorted(MODELS.items()):
        descriptors.append(types.Tool(name=name,
            description={
                'hwpx_health': 'Backend availability and queue status; no document opened.',
                'hwpx_open': 'Upload an allowlisted local file to a server-managed copy. One active backend document; never edits the input file.',
                'hwpx_status': 'Inspect explicit session and reconciliation status. May repair stale backend status; not a rollback claim.',
                'hwpx_find': 'Find text in the explicit managed copy; may move the live cursor or create proof.',
                'hwpx_where': 'Read native cursor/location; runs work in the existing serialized backend lane.',
                'hwpx_command': 'Bounded context/readback/selection, guarded cell alignment or margin formatting, or command reconciliation. No arbitrary COM, Python or shell.',
                'hwpx_proof': 'Native frame or one PDF-rendered page. Single-page proof does not establish full-document visual QA.',
                'hwpx_save': 'Save the managed working copy only; retrieve its download artifact before close.',
                'hwpx_close': 'Close the explicit managed session and remove its managed artifacts. Download wanted output first.',
            }[name], input_schema=model.model_json_schema(), output_schema=Envelope.model_json_schema(),
            annotations=types.ToolAnnotations(read_only_hint=name == 'hwpx_health',
                destructive_hint=name != 'hwpx_health', idempotent_hint=name == 'hwpx_health', open_world_hint=False)))

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=descriptors)

    async def call_tool(ctx, params):
        model = MODELS.get(params.name)
        if model is None:
            raise MCPError(code=-32602, message='Unknown HWPX tool')
        try:
            args = model.model_validate(params.arguments if params.arguments is not None else {})
        except ValidationError as exc:
            # Arguments failing a tool's input schema are execution errors per
            # MCP tools Error Handling, distinct from malformed CallToolRequest.
            value = envelope(params.name, error={'code': 'INVALID_ARGUMENTS',
                'message': 'Tool arguments do not satisfy the closed schema.',
                'details': {'fields': [list(e['loc']) for e in exc.errors()][:16]}})
        else:
            value = await facade.execute(params.name, args)
        return types.CallToolResult(content=[types.TextContent(text=json.dumps(value, ensure_ascii=False))],
                                    structured_content=value, is_error=not value['ok'])

    server = Server('hwpx-mcp-adapter', version='0.1.0', on_list_tools=list_tools, on_call_tool=call_tool)
    app = server.streamable_http_app(streamable_http_path='/mcp', json_response=False, stateless_http=True,
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[f'127.0.0.1:{settings.port}', f'localhost:{settings.port}'],
            allowed_origins=[f'http://127.0.0.1:{settings.port}', f'http://localhost:{settings.port}']))
    return Boundary(app, settings)
