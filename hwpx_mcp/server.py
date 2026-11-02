"""Opt-in, loopback-only MCP facade over the existing HWPX REST service.

No COM, shared CLI cache, installer hooks or background backend process is created here.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any
from urllib.parse import urlsplit
import uuid
import zipfile

import httpx2
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import InboundLadderRejection, classify_inbound_request
import mcp.types as types
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from hwpx_mcp import review as review_rules
from hwpx_mcp.proof import ProofError
from hwpx_mcp.schema import MODELS, Envelope

PROTOCOL_VERSION = '2026-07-28'
MAX_BODY = 1024 * 1024
MAX_BACKEND = 4 * 1024 * 1024
MAX_FILE = 25 * 1024 * 1024
MAX_INLINE_IMAGE = 3 * 1024 * 1024
# A saved document may outgrow the input limit; it must still be retrievable, or the close guard would never release it.
MAX_SAVED_FILE = 256 * 1024 * 1024


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


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def discard(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # Its name is one no consumer accepts; a leftover is never treated as output.


def write_json_atomic(path, value):
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class BackendFailure(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)


def envelope(operation, *, sid=None, result=None, error=None):
    return Envelope(ok=error is None, operation=operation, session_id=sid,
                    document_id=sid, result=result, error=error).model_dump(mode='json')


def rejection_summary(value: dict[str, Any]) -> dict[str, Any]:
    """What stopped the operation, in the first fields a caller reads, from the backend's own fixed labels."""
    steps = value.get('steps')
    failed = next((step for step in steps if isinstance(step, dict) and step.get('ok') is False), {}) if isinstance(steps, list) else {}
    diagnostic = failed.get('failure_diagnostic') if isinstance(failed.get('failure_diagnostic'), dict) else {}
    detail = value.get('detail') if isinstance(value.get('detail'), str) else None
    coded = re.match(r'([a-z_]+): ', detail) if detail else None
    summary = {
        'reason': failed.get('refusal_code') or diagnostic.get('numbering_reason') or (coded.group(1) if coded else detail),
        'message': failed.get('refusal_message') or (detail if coded else None),
        'stopped_before_edit': failed.get('mutation_may_have_persisted') is False if failed else None,
        'found_page': failed.get('found_page'),
        'session_held': value.get('native_custody_held') is True or (coded is not None and coded.group(1) == 'native_custody_held'),
    }
    return {key: item for key, item in summary.items() if item is not None}


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
                summary = rejection_summary(value)
                if summary.get('reason') == 'save_required':
                    raise BackendFailure('SAVE_REQUIRED', 'The document has unsaved changes. Call hwpx_save, then close it.',
                                         {'http_status': response.status_code, **summary, 'backend': value})
                raise BackendFailure('BACKEND_REJECTED', 'Backend rejected the operation; inspect its evidence before retrying.',
                                     {'http_status': response.status_code, **summary, 'backend': value})
            return value

    async def session(self, http, sid):
        value = await self.request(http, 'GET', '/interactive/session/status', params={'session_id': sid})
        record = value.get('session')
        if not isinstance(record, dict) or record.get('session_id') != sid:
            raise BackendFailure('SESSION_IDENTITY_MISMATCH', 'Backend session identity did not match the explicit handle.')
        metadata = record.get('metadata')
        local = metadata.get('local_cli_v1') if isinstance(metadata, dict) else None
        # Only the local-CLI open producer establishes managed origin.  Bridge
        # and close markers are additive lifecycle facts, never admission proof.
        if not isinstance(local, dict) or local.get('opened_via') != 'local_cli_v1':
            raise BackendFailure('SESSION_NOT_MANAGED', 'Only server-managed local-CLI working-copy sessions are supported.')
        return value

    @staticmethod
    def _session_is_closed(value: dict[str, Any]) -> bool:
        record = value.get('session') if isinstance(value.get('session'), dict) else {}
        state = str(record.get('state') or '').strip().casefold()
        return state in {'closed', 'closed_cleanup_pending'} or bool(record.get('closed_at'))

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
                    # A saved output under the artifact root can be reopened to check what actually persisted.
                    if (not (source.is_relative_to(self.settings.source_root.resolve()) or source.is_relative_to(self.settings.artifact_root.resolve())) or
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
                    try:
                        await asyncio.to_thread(self.record_source, sid, source)
                    except OSError:
                        # The session is open and its identity must reach the caller; only a later comparison is lost.
                        result = {**result, 'source_record_error': 'SOURCE_RECORD_NOT_WRITTEN'}
                else:
                    record = await self.session(http, sid)
                    if name == 'hwpx_status':
                        # A closed session is still queryable by explicit identity.
                        result = record
                        live = await self.request(http, 'GET', '/local-cli/status')
                        if live.get('session_id') == sid:
                            result = {**record, 'runtime': live}
                    elif name == 'hwpx_command':
                        if self._session_is_closed(record):
                            raise BackendFailure('SESSION_CLOSED', 'Closed local-CLI sessions are status-queryable but cannot receive new commands.')
                        request = args.request.model_dump(exclude_none=True)
                        if request['op'] == 'command_reconcile':
                            result = await self.request(http, 'POST', '/local-cli/command-reconcile',
                                json={'session_id': sid, 'command_id': request['command_id']})
                        elif request['op'] in {'undo', 'redo'}:
                            result = await self.request(http, 'POST', '/local-cli/' + request['op'], json={'session_id': sid})
                        else:
                            # Reuse the strict existing bundle validation, including
                            # each command's native CAS/proof guard. Never widen it.
                            from local_cli_v1.bundles import CommandStep
                            fields = {k: v for k, v in request.items() if k not in {'op', 'label'}}
                            step = CommandStep(op=request['op'], label=request['label'], fields=fields)
                            result = await self.request(http, 'POST', '/local-cli/command-bundle',
                                json={'session_id': sid, 'steps': [step.to_server_json()]})
                    elif name == 'hwpx_proof':
                        if self._session_is_closed(record):
                            raise BackendFailure('SESSION_CLOSED', 'Closed local-CLI sessions are status-queryable but cannot receive new commands.')
                        if args.request.kind == 'frame':
                            result = await self.request(http, 'POST', '/local-cli/screenshot', json={'session_id': sid})
                        elif args.request.kind == 'review':
                            result = await self.review(http, sid, args.request)
                        else:
                            result = await self.page_proof(http, sid, args.request.page, args.request.dpi)
                    else:
                        if self._session_is_closed(record):
                            raise BackendFailure('SESSION_CLOSED', 'Closed local-CLI sessions are status-queryable but cannot receive new commands.')
                        payload = {'session_id': sid}
                        if name == 'hwpx_find':
                            payload.update(args.request.model_dump(exclude_none=True))
                        if name == 'hwpx_cell_margins_get':
                            result = await self.cell_margins_get(http, sid, args)
                        else:
                            saved = await self.close_guard(http, sid) if name == 'hwpx_close' else None
                            result = await self.request(http, 'POST', '/local-cli/' + name.removeprefix('hwpx_'), json=payload)
                            if name == 'hwpx_save':
                                result = {**result, 'saved_output': await self.saved_output(http, sid, result)}
                            elif name == 'hwpx_close':
                                result = {**result, 'saved_output': saved}
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
        except ProofError as exc:
            return envelope(name, sid=sid, error={'code': 'RENDER_FAILED', 'message': str(exc), 'details': {}})
        except (OSError, ValueError):
            return envelope(name, sid=sid, error={'code': 'LOCAL_INPUT_OR_ARTIFACT_ERROR', 'message': 'Input or artifact processing failed; no automatic replay was attempted.', 'details': {}})
        finally:
            self.serial.release()

    async def cell_margins_get(self, http, sid, args):
        """One canonical POST plus full positive-response validation.

        A fabricated ``ok=true`` backend body with missing or mismatched
        getter proof is BACKEND_INVALID_RESPONSE, never a success.
        """

        from app.models import canonical_cell_margins_request_sha256

        request_sha256 = canonical_cell_margins_request_sha256(args)
        result = await self.request(http, 'POST', '/local-cli/cell-margins-get',
                                    json=args.model_dump(mode='json'))
        if not isinstance(result.get('ok'), bool):
            raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                 'Getter response is missing a valid positive flag.')
        if result.get('session_id') != sid or result.get('document_id') != sid:
            raise BackendFailure('SESSION_IDENTITY_MISMATCH',
                                 'Getter response identity did not match the requested handle.')
        if result.get('request_sha256') != request_sha256:
            raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                 'Getter response digest does not bind the submitted request.')
        if result.get('document_generation') is None or result.get('working_copy_file') is None:
            raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                 'Getter response is missing its observation basis.')
        state = result.get('state')
        if not isinstance(state, dict):
            raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                 'Getter response is missing its state projection.')
        if result.get('ok') is True:
            if (result.get('ok') is not True or result.get('semantic_ok') is not True
                    or result.get('read_only') is not True
                    or result.get('dirty') is not False
                    or result.get('may_have_mutated') is not False
                    or result.get('mutation_may_have_persisted') is not False
                    or result.get('margins_hu') is None
                    or state.get('document_state_unchanged') is not True
                    or state.get('navigation_restored') is not True
                    or state.get('mutation_attempted') is not False
                    or state.get('document_modified_before') is not state.get('document_modified_after')
                    or result.get('observation') is not None
                    or result.get('error') is not None):
                raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                     'Getter success response violates positive invariants.')
            margins = result['margins_hu']
            if not isinstance(margins, dict) or set(margins) != {'left', 'right', 'top', 'bottom'}:
                raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                     'Getter response must carry exactly four margin sides.')
            for side, value in margins.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                         'Getter margin values must be finite nonnegative numbers.',
                                         {'side': side})
            provenance = result.get('provenance')
            if (not isinstance(provenance, dict)
                    or provenance.get('cache_used') is not False
                    or provenance.get('refresh_succeeded') is not True
                    or provenance.get('refresh_method') != 'HAction.GetDefault'):
                raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                     'Getter provenance must show one fresh native refresh.')
            command = result.get('command')
            if (not isinstance(command, dict)
                    or not isinstance(command.get('command_id'), str) or not command['command_id']
                    or isinstance(command.get('sequence'), bool)
                    or not isinstance(command.get('sequence'), int) or command['sequence'] <= 0
                    or command.get('state') != 'succeeded'):
                raise BackendFailure('BACKEND_INVALID_RESPONSE',
                                     'Getter success requires a submitted succeeded command.')
        return result

    async def fetch_complete(self, http, route, partial, what, limit=MAX_FILE):
        """Stream to a name no consumer accepts and check the length, so an interrupted copy can never pass as a file."""
        async with http.stream('GET', self.settings.backend.rstrip('/') + route) as response:
            if response.status_code != 200:
                # Only a 404 says there is nothing to retrieve; a 409 is the backend refusing a copy it cannot vouch for.
                code = {404: 'ARTIFACT_NOT_FOUND', 409: 'ARTIFACT_CUSTODY_MISMATCH'}.get(response.status_code, 'ARTIFACT_DOWNLOAD_FAILED')
                raise BackendFailure(code, f'{what} could not be retrieved.')
            declared = response.headers.get('content-length')
            size = 0
            with partial.open('xb') as stream:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise BackendFailure('ARTIFACT_SIZE_LIMIT', f'{what} exceeds the size limit and was not kept.')
                    stream.write(chunk)
            if declared is not None and declared.isdigit() and int(declared) != size:
                raise BackendFailure('ARTIFACT_INCOMPLETE', f'{what} arrived incomplete and was not kept.')

    async def download(self, http, route, target, what):
        partial = target.with_name(target.name + '.part')
        try:
            await self.fetch_complete(http, route, partial, what)
            os.replace(partial, target)
        finally:
            discard(partial)

    async def export_pdf(self, http, sid, pdf):
        result = await self.request(http, 'POST', '/local-cli/export', json={'session_id': sid})
        expected = f'/local-cli/session/{sid}/artifact/export'
        if result.get('download_path') != expected:
            raise BackendFailure('ARTIFACT_IDENTITY_MISMATCH', 'Export returned an unexpected artifact route.')
        await self.download(http, expected, pdf, 'Native PDF artifact')
        return result

    def session_dir(self, sid):
        return self.settings.artifact_root / sid

    def record_source(self, sid, source):
        """Remember what was opened, so a later saved output can be compared with it. The session is open either way."""
        folder = self.session_dir(sid)
        folder.mkdir(parents=True, exist_ok=True)
        record = {'path': str(source), 'sha256': file_sha256(source), 'package': None, 'package_error': None}
        if source.suffix.lower() == '.hwpx':
            try:
                record['package'] = review_rules.package_inventory(source)
            except review_rules.PackageUnreadable as exc:
                record['package_error'] = exc.code
        else:
            record['package_error'] = 'not_hwpx'
        write_json_atomic(folder / 'source.json', record)

    def saved_name(self, sid, result=None):
        name = (result or {}).get('filename')
        if not name:
            source = (self.read_json(self.session_dir(sid) / 'source.json') or {}).get('path')
            name = f'{Path(source).stem}-edited{Path(source).suffix}' if source else 'document.hwpx'
        return Path(str(name)).name

    async def publish_saved(self, http, sid, name):
        """Publish only a complete, verified copy, atomically, and then point the latest-saved record at it."""
        route = f'/local-cli/session/{sid}/artifact/working-copy'
        folder = self.session_dir(sid) / 'saved'
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / f'.incoming-{uuid.uuid4().hex}.part'
        try:
            await self.fetch_complete(http, route, partial, 'Saved document', limit=MAX_SAVED_FILE)
            return await self.publish_verified(sid, partial, name)
        finally:
            discard(partial)

    async def publish_verified(self, sid, partial, name):
        folder = self.session_dir(sid) / 'saved'
        package, package_error = None, None
        if Path(name).suffix.lower() == '.hwpx':
            try:
                package = await asyncio.to_thread(review_rules.package_inventory, partial)
            except review_rules.PackageUnreadable as exc:
                if exc.code != 'package_limit':
                    raise BackendFailure('ARTIFACT_CORRUPT', 'Saved document did not arrive as a readable package and was not kept.') from None
                package_error = exc.code
            if package is not None and package['sections'] < 1:
                raise BackendFailure('ARTIFACT_CORRUPT', 'Saved document has no document section and was not kept.')
        numbers = [int(match.group(1)) for item in folder.iterdir() if (match := re.match(r'([0-9]+)-', item.name))]
        target = folder / f'{max(numbers, default=0) + 1:03d}-{name}'
        saved = {'retrieved': True, 'path': str(target), 'sha256': await asyncio.to_thread(file_sha256, partial), 'size_bytes': partial.stat().st_size, 'package': package, 'package_error': package_error}
        source_record = self.read_json(self.session_dir(sid) / 'source.json')
        if package is not None and source_record and source_record.get('package'):
            saved['compared_with_source'] = review_rules.compare_packages(source_record['package'], package)
        os.replace(partial, target)
        write_json_atomic(self.session_dir(sid) / 'latest-saved.json', saved)
        return saved

    def record_saved_failure(self, sid, code, message):
        """The older copy, if any, is no longer the latest; say so where review and close will read it."""
        folder = self.session_dir(sid)
        previous = self.read_json(folder / 'latest-saved.json') or {}
        last = previous if previous.get('retrieved') is True else previous.get('last_retrieved')
        failure = {
            'retrieved': False, 'code': code, 'message': message, 'close_blocked': True,
            'retry': 'Call hwpx_save again. The document stays open and its saved copy stays in the managed session until a copy succeeds.',
            'last_retrieved': {key: last[key] for key in ('path', 'sha256', 'size_bytes')} if last else None,
        }
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_json_atomic(folder / 'latest-saved.json', failure)
        except OSError:
            pass  # The returned failure still carries it; the close guard fetches again regardless.
        return failure

    async def saved_output(self, http, sid, result):
        """The native save already succeeded; a failed copy is reported beside it and never turns it into a failed save."""
        try:
            if result.get('download_path') != f'/local-cli/session/{sid}/artifact/working-copy':
                raise BackendFailure('ARTIFACT_IDENTITY_MISMATCH', 'Save returned an unexpected artifact route.')
            return await self.publish_saved(http, sid, self.saved_name(sid, result))
        except BackendFailure as exc:
            return self.record_saved_failure(sid, exc.code, exc.message)
        except (httpx2.TimeoutException, httpx2.TransportError, OSError):
            return self.record_saved_failure(sid, 'ARTIFACT_DOWNLOAD_FAILED', 'Saved document could not be copied to the artifact root.')

    async def close_guard(self, http, sid):
        """Close deletes the managed working copy, so its content must already exist outside it, verified, or close is refused."""
        live = await self.request(http, 'GET', '/local-cli/status')
        held = live.get('session_id') == sid and live.get('native_custody_held') is True
        if live.get('session_id') == sid and live.get('working_copy_dirty') is True:
            return self.read_json(self.session_dir(sid) / 'latest-saved.json')  # The backend refuses this close with its save reason.
        record = self.read_json(self.session_dir(sid) / 'latest-saved.json')
        folder = self.session_dir(sid) / 'saved'
        partial = folder / f'.incoming-{uuid.uuid4().hex}.part'
        try:
            folder.mkdir(parents=True, exist_ok=True)
            try:
                await self.fetch_complete(http, f'/local-cli/session/{sid}/artifact/working-copy', partial, 'Saved document', limit=MAX_SAVED_FILE)
            except BackendFailure as exc:
                if exc.code == 'ARTIFACT_NOT_FOUND' and record is None:
                    return None  # No save was recorded and the backend holds no saved copy.
                raise
            digest = await asyncio.to_thread(file_sha256, partial)
            latest = record if record and record.get('retrieved') is True else (record or {}).get('last_retrieved')
            if latest and latest.get('sha256') == digest and Path(latest['path']).is_file() and await asyncio.to_thread(file_sha256, Path(latest['path'])) == digest:
                if record.get('retrieved') is not True:
                    write_json_atomic(self.session_dir(sid) / 'latest-saved.json', {'retrieved': True, **latest})
                return self.read_json(self.session_dir(sid) / 'latest-saved.json')
            source = self.read_json(self.session_dir(sid) / 'source.json')
            if record is None and source and source.get('sha256') == digest:
                return None  # Unchanged since it was opened; the input file already holds this content.
            return await self.publish_verified(sid, partial, self.saved_name(sid))
        except BackendFailure as exc:
            failure = self.record_saved_failure(sid, exc.code, exc.message)
        except (httpx2.TimeoutException, httpx2.TransportError, OSError):
            failure = self.record_saved_failure(sid, 'ARTIFACT_DOWNLOAD_FAILED', 'Saved document could not be copied to the artifact root.')
        finally:
            discard(partial)
        raise BackendFailure('SAVED_OUTPUT_NOT_RETRIEVED',
                             'The saved document could not be copied out, so the session was not closed and nothing was deleted. '
                             + ('The session is held, so it stays retained.' if held else 'Call hwpx_save or hwpx_close again.'),
                             {'session_held': held, 'saved_output': failure})

    @staticmethod
    def read_json(path):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def review_baseline(self, sid, compare_to):
        if compare_to is not None:
            owner, name = compare_to.split('/', 1)
            baseline = self.read_json(self.settings.artifact_root / owner / name / 'review.json')
            if baseline is None:
                raise BackendFailure('REVIEW_NOT_FOUND', 'The requested earlier review does not exist under the artifact root.')
            return baseline
        return self.read_json(self.session_dir(sid) / 'review-1' / 'review.json')

    async def review(self, http, sid, request):
        """One readable answer: what was covered, numbering as printed, changes and preservation, previews, saved file."""
        readback = await self.request(http, 'POST', '/local-cli/numbering-readback', json={'session_id': sid})
        folder = self.session_dir(sid)
        folder.mkdir(parents=True, exist_ok=True)
        existing = [int(match.group(1)) for item in folder.iterdir() if (match := re.fullmatch(r'review-([0-9]+)', item.name))]
        number = max(existing, default=0) + 1
        baseline = self.review_baseline(sid, request.compare_to)
        destination = folder / f'review-{number}'
        destination.mkdir(exist_ok=False)
        pdf = destination / 'document.pdf'
        pages, texts, render_error = [], None, None
        from hwpx_mcp.proof import pdf_page_texts, render_pages
        try:
            await self.export_pdf(http, sid, pdf)
            pages = await asyncio.to_thread(render_pages, pdf, destination, dpi=request.dpi, max_pages=request.max_pages)
            texts = await asyncio.to_thread(pdf_page_texts, pdf, max_pages=request.max_pages)
        except ProofError:
            render_error = 'RENDER_FAILED'
        except BackendFailure as exc:
            if exc.details.get('session_held'):
                raise
            render_error = exc.details.get('reason') or exc.code
        from PIL import Image
        page_rows = []
        for index, png in enumerate(pages, start=1):
            with Image.open(png) as image:
                width, height = image.size
                image.verify()
            page_rows.append({'page': index, 'path': str(png), 'sha256': await asyncio.to_thread(file_sha256, png), 'width': width, 'height': height})
        saved_record = self.read_json(folder / 'latest-saved.json')
        current = review_rules.assemble(
            session_id=sid, review_id=f'{sid}/review-{number}', readback=readback,
            pdf={'path': str(pdf), 'sha256': await asyncio.to_thread(file_sha256, pdf)} if pdf.is_file() else None,
            pages=page_rows, texts=texts, baseline=baseline, saved_output=saved_record, render_error=render_error,
            persisted_format=await self.persisted_format(folder, readback, saved_record),
            persisted_tables=await self.persisted_tables(folder, readback, saved_record),
        )
        summary = destination / 'summary.md'
        current['summary_path'] = str(summary)
        current['json_path'] = str(destination / 'review.json')
        summary.write_text(review_rules.to_markdown(current), encoding='utf-8')
        write_json_atomic(destination / 'review.json', current)
        # The structured result stays small; the full paragraph and table lists are in review.json.
        return {'ok': True, 'session_id': sid, **{key: value for key, value in current.items() if key not in {'paragraphs', 'tables'}}}

    async def persisted_tables(self, folder, readback, saved_record):
        """The tables a saved file itself holds against the unmodified live document: the latest copy, or the saved copy this session reopened."""
        tables = readback.get('tables')
        if readback.get('document_is_modified') is not False or tables is None:
            return None
        kept = saved_record.get('path') if saved_record and saved_record.get('retrieved') is True else None
        compared_with = 'saved_output'
        if kept is None:
            opened = (self.read_json(folder / 'source.json') or {}).get('path')
            kept = opened if opened and Path(opened).is_relative_to(self.settings.artifact_root.resolve()) else None
            compared_with = 'reopened_saved_file'
        if kept is None or Path(kept).suffix.lower() != '.hwpx':
            return None
        value = await asyncio.to_thread(review_rules.persisted_tables, Path(kept), tables, truncated=readback.get('tables_truncated') is True)
        return {**value, 'compared_with': compared_with, 'path': str(kept)}

    async def persisted_format(self, folder, readback, saved_record):
        """The formatting a saved file itself holds against the unmodified live document: the latest copy, or the saved copy this session reopened."""
        paragraphs = readback.get('paragraphs')
        if readback.get('document_is_modified') is not False or not paragraphs or not all('format' in item for item in paragraphs):
            return None
        kept = saved_record.get('path') if saved_record and saved_record.get('retrieved') is True else None
        compared_with = 'saved_output'
        if kept is None:
            opened = (self.read_json(folder / 'source.json') or {}).get('path')
            kept = opened if opened and Path(opened).is_relative_to(self.settings.artifact_root.resolve()) else None
            compared_with = 'reopened_saved_file'
        if kept is None or Path(kept).suffix.lower() != '.hwpx':
            return None
        value = await asyncio.to_thread(review_rules.persisted_format, Path(kept), paragraphs, truncated=readback.get('paragraphs_truncated') is True)
        return {**value, 'compared_with': compared_with, 'path': str(kept)}

    async def page_proof(self, http, sid, page, dpi):
        destination = self.settings.artifact_root / sid / uuid.uuid4().hex
        destination.mkdir(parents=True, exist_ok=False)
        pdf = destination / 'document.pdf'
        png = destination / f'page-{page}.png'
        result = await self.export_pdf(http, sid, pdf)
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


def review_content(result, inline_images):
    """The short summary as its own text, then the first page previews a client can show directly."""
    content = [types.TextContent(text=result['summary_text'])]
    for page in result['pages'][:inline_images]:
        raw = Path(page['path']).read_bytes()
        if len(raw) <= MAX_INLINE_IMAGE:
            content.append(types.ImageContent(data=base64.b64encode(raw).decode('ascii'), mime_type='image/png'))
    return content


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
                'hwpx_command': 'Bounded context/readback/selection, guarded cell alignment or margin formatting, native page-number insertion, native outline numbering of one proven paragraph, insertion or removal of one proven body paragraph (removal also requires its full text), character formatting (font family, size, bold, italic, underline, text color; a printed number mark stays as it was unless number_mark=follow) or paragraph formatting (alignment, indents, line spacing) of one exact range or paragraph found again before each command, tables (table_inventory and table_read to list and read them; table_edit_exact to insert or delete rows and columns, merge, split or unmerge cells; table_style_exact to set border line type, width and color on edges and set or clear the cell fill of one cell, a range or the whole table; table_create_exact to add a table after a paragraph), undo or redo within the tracked edits of this session with a report of what changed, or command reconciliation. No arbitrary COM, Python or shell.',
                'hwpx_proof': 'Native frame, one PDF-rendered page, or kind=review: first how much could be checked (complete, partial, unverified or mismatch), then numbering as printed in the native PDF, changes and preservation against an earlier review, page previews, the PDF and the saved file. Does not edit or save.',
                'hwpx_save': 'Save the managed working copy and copy the saved file to the artifact root; the result gives its path, hash and a comparison with the opened file. If the copy fails the result says so, and calling hwpx_save again retries it.',
                'hwpx_close': 'Close the explicit clean managed session and remove its managed files, after confirming its saved content exists under the artifact root. Refused, with nothing deleted, while unsaved changes remain or the saved file cannot be copied.',
                'hwpx_cell_margins_get': 'One exact-target four-side cell-margin observation in the existing live managed session; navigation is restored and no cache, persistence or freshness guarantee is implied.',
            }[name], input_schema=model.model_json_schema(), output_schema=Envelope.model_json_schema(),
            annotations=types.ToolAnnotations(read_only_hint=name in {'hwpx_health', 'hwpx_cell_margins_get'},
                destructive_hint=name not in {'hwpx_health', 'hwpx_cell_margins_get'},
                idempotent_hint=name == 'hwpx_health', open_world_hint=False)))

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
        content = [types.TextContent(text=json.dumps(value, ensure_ascii=False))]
        if value['ok'] and params.name == 'hwpx_proof' and args.request.kind == 'review':
            content.extend(review_content(value['result'], args.request.inline_images))
        return types.CallToolResult(content=content, structured_content=value, is_error=not value['ok'])

    server = Server('hwpx-mcp-adapter', version='0.1.0', on_list_tools=list_tools, on_call_tool=call_tool)
    app = server.streamable_http_app(streamable_http_path='/mcp', json_response=False, stateless_http=True,
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[f'127.0.0.1:{settings.port}', f'localhost:{settings.port}'],
            allowed_origins=[f'http://127.0.0.1:{settings.port}', f'http://localhost:{settings.port}']))
    return Boundary(app, settings)
