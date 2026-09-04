from __future__ import annotations

import hashlib
import copy
from contextlib import nullcontext
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.atomic_json import atomic_write_json, path_lock, update_json_object
from app.interactive.operator_status import render_operator_status
from app.interactive.verify_evidence import build_verify_step_binding, resolve_verify_capture_timing
from app.logging_utils import configure_logger
from app.observation import ensure_viewer_session, latest_frame_metadata_path, latest_frame_path, load_viewer_session
from app.readiness import (
    build_plain_readiness_failure,
    load_runtime_readiness_snapshot,
    readiness_matches_current_worker,
)

COMMAND_SEQUENCE = (
    'open',
    'status',
    'find',
    'choose',
    'enter',
    'lock',
    'verify-pre',
    'apply',
    'verify-post',
    'undo',
    'close',
)
PROGRESS_COMMAND_SEQUENCE = tuple(command for command in COMMAND_SEQUENCE if command != 'status')
TERMINAL_SESSION_STATES = {'closed', 'failed'}
VERIFY_EVIDENCE_SOFT_WARN_AGE_MS = 5_000
VERIFY_EVIDENCE_HARD_STALE_AGE_MS = 30_000
VERIFY_EVIDENCE_RETENTION_SCHEMA_VERSION = 'interactive-verify-evidence-retention/v1'
VERIFY_EVIDENCE_RETENTION_LEDGER_SCHEMA_VERSION = 'interactive-verify-evidence-retention-ledger/v1'
VERIFY_EVIDENCE_RETENTION_LEDGER_MAX_ENTRIES = 100
MAX_INTERACTIVE_COMMAND_HISTORY = 100
MAX_SETTLED_INTERACTIVE_SESSIONS = 100
MAX_INTERACTIVE_HISTORY_VALUE_CHARS = 2048
MAX_INTERACTIVE_HISTORY_ITEMS = 100
MAX_INTERACTIVE_EVENT_FILE_BYTES = 64 * 1024
MAX_INTERACTIVE_EVENT_FILES = 5
MAX_INTERACTIVE_STATE_FILE_BYTES = 4 * 1024 * 1024
_SENSITIVE_HISTORY_KEY_MARKERS = (
    'text', 'content', 'preview', 'query', 'path', 'filename', 'document',
    'token', 'secret', 'password', 'cookie', 'credential', 'exception', 'raw',
)
_HISTORY_SECRET_PATTERN = re.compile(
    r'(?i)\b(password|token|secret|cookie|credential)\s*[:=]\s*[^\s,;]+'
)
_HISTORY_LOCAL_PATH_PATTERN = re.compile(
    r'''(?<![\w:/])(?:[A-Za-z]:[\\/]|/(?!/))[^\s"'<>]+'''
)
_SAFE_COMMAND_HISTORY_KEYS = frozenset({
    'state', 'status', 'ok', 'dirty', 'outcome', 'command', 'result_state',
    'candidate_count', 'match_count', 'selected_candidate_index', 'page',
    'requested_page', 'dpi', 'width', 'height', 'position', 'embedded',
    'treat_as_char', 'resolved_target_id', 'operation', 'code', 'reason_code',
    'failure_code', 'step', 'index', 'count', 'total', 'matched', 'found',
    'available', 'ready', 'proof_fresh', 'reconciled', 'timed_out', 'sequence',
    'command_id', 'session_id', 'verification_state', 'capture_state',
})
_SAFE_COMMAND_HISTORY_STRING_KEYS = frozenset({
    'state', 'status', 'outcome', 'command', 'result_state', 'position',
    'resolved_target_id', 'operation', 'code', 'reason_code', 'failure_code',
    'step', 'command_id', 'session_id', 'verification_state', 'capture_state',
})


class InteractiveSessionError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _datetime_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _json_dump(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if not isinstance(payload, dict):
        raise InteractiveSessionError('interactive JSON state must be an object')
    if len(serialized.encode('utf-8')) > MAX_INTERACTIVE_STATE_FILE_BYTES:
        raise InteractiveSessionError(f'interactive session state exceeds {MAX_INTERACTIVE_STATE_FILE_BYTES} bytes')
    atomic_write_json(path, payload)


def _json_load(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or path.stat().st_size > MAX_INTERACTIVE_STATE_FILE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _append_jsonl(path: Path, payload: object) -> None:
    bounded = _bounded_history_value(payload)
    serialized = json.dumps(bounded, ensure_ascii=False, separators=(',', ':')) + '\n'
    encoded = serialized.encode('utf-8')
    if len(encoded) > MAX_INTERACTIVE_EVENT_FILE_BYTES:
        serialized = json.dumps({
            'schema_version': 'interactive/event-omitted/v1',
            'payload_sha256': hashlib.sha256(encoded).hexdigest(),
            'reason': 'event exceeded bounded serialized size',
        }, ensure_ascii=True, separators=(',', ':')) + '\n'
        encoded = serialized.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rotation and append are one transaction.  Without a process lock, two
    # concurrent command requests can both rotate the same generation and
    # either lose an event or interleave bytes in a JSONL record.
    with path_lock(path):
        current_size = path.stat().st_size if path.exists() else 0
        if current_size + len(encoded) > MAX_INTERACTIVE_EVENT_FILE_BYTES:
            path.with_name(f'{path.name}.{MAX_INTERACTIVE_EVENT_FILES}').unlink(missing_ok=True)
            for index in range(MAX_INTERACTIVE_EVENT_FILES - 1, 0, -1):
                source = path.with_name(f'{path.name}.{index}')
                target = path.with_name(f'{path.name}.{index + 1}')
                if source.exists():
                    os.replace(source, target)
            if path.exists():
                os.replace(path, path.with_name(f'{path.name}.1'))
        with path.open('ab') as handle:
            handle.write(encoded)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith('Z'):
        normalized = normalized[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sanitize_history_text(value: Any) -> str:
    text = str(value)
    text = _HISTORY_SECRET_PATTERN.sub(lambda match: f'{match.group(1)}=<redacted>', text)
    text = _HISTORY_LOCAL_PATH_PATTERN.sub('<redacted-path>', text)
    if len(text) > MAX_INTERACTIVE_HISTORY_VALUE_CHARS:
        return text[:MAX_INTERACTIVE_HISTORY_VALUE_CHARS] + '…'
    return text


def _bounded_history_value(value: Any, *, depth: int = 0) -> Any:
    """Keep operator command history bounded without retaining raw document text."""

    if depth >= 5:
        return '<nested value omitted>'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_history_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_INTERACTIVE_HISTORY_ITEMS:
                result['__truncated__'] = True
                break
            key_text = str(key)[:MAX_INTERACTIVE_HISTORY_VALUE_CHARS]
            if any(marker in key_text.casefold() for marker in _SENSITIVE_HISTORY_KEY_MARKERS):
                result[key_text] = '<redacted>'
            else:
                result[key_text] = _bounded_history_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_bounded_history_value(item, depth=depth + 1) for item in value[:MAX_INTERACTIVE_HISTORY_ITEMS]]
        if len(value) > MAX_INTERACTIVE_HISTORY_ITEMS:
            result.append('<items omitted>')
        return result
    return _bounded_history_value(repr(value), depth=depth + 1)


def _opaque_history_marker(value: Any, *, reason: str) -> dict[str, str | bool]:
    """Retain only a bounded digest when a history field is not safe to keep."""

    try:
        representation = repr(value)
    except Exception:
        representation = type(value).__name__
    digest = hashlib.sha256(
        representation[:MAX_INTERACTIVE_HISTORY_VALUE_CHARS].encode('utf-8', errors='replace')
    ).hexdigest()
    return {
        'omitted': True,
        'reason': reason,
        'sha256': digest,
    }


def _bounded_command_history_payload(command: str, payload: Any) -> dict[str, Any]:
    """Apply a command-specific allowlist before durable history storage.

    Interactive command payloads often contain candidate text, document
    previews, local paths, or API response objects.  History only needs
    bounded operator state/counters; every other field is represented by a
    digest so it cannot become a second document store.
    """

    if not isinstance(payload, dict):
        return {'omitted_payload': _opaque_history_marker(payload, reason='payload_not_mapping')}
    bounded: dict[str, Any] = {'command': _sanitize_history_text(command)}
    for index, (raw_key, value) in enumerate(payload.items()):
        if index >= MAX_INTERACTIVE_HISTORY_ITEMS:
            bounded['__truncated__'] = True
            break
        key = str(raw_key)[:MAX_INTERACTIVE_HISTORY_VALUE_CHARS]
        folded = key.casefold()
        if key not in _SAFE_COMMAND_HISTORY_KEYS:
            reason = 'sensitive_field' if any(marker in folded for marker in _SENSITIVE_HISTORY_KEY_MARKERS) else 'not_allowlisted'
            bounded[key] = _opaque_history_marker(value, reason=reason)
            continue
        if isinstance(value, str):
            if key not in _SAFE_COMMAND_HISTORY_STRING_KEYS:
                bounded[key] = _opaque_history_marker(value, reason='string_field_not_allowlisted')
            else:
                bounded[key] = _sanitize_history_text(value)
        elif value is None or isinstance(value, (bool, int, float)):
            bounded[key] = value
        else:
            bounded[key] = _opaque_history_marker(value, reason='non_scalar_field')
    return bounded


def _timestamp_slug(value: str) -> str:
    slug = ''.join(character if character.isalnum() else '-' for character in str(value))
    slug = slug.strip('-')
    return slug[:80] or 'step'


def _existing_file(value: Any) -> Path | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value)
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise InteractiveSessionError(f'Interactive evidence file could not be read: {path}') from exc
    return size, digest.hexdigest()


def _canonical_payload_sha256(payload: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise InteractiveSessionError('Interactive evidence metadata is not strictly serializable.') from exc
    return hashlib.sha256(encoded).hexdigest()


def _api_base_url(settings: Settings) -> str:
    return f'http://{settings.api_host}:{settings.api_port}'


class InteractiveSessionManager:
    """Single-session `.51` interactive state holder.

    This is intentionally a thin API-side scaffold.
    It keeps operator-visible session/command telemetry first-class while the live Hancom
    command executor is still being wired in a later slice.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.logger = configure_logger(
            'hwp.interactive',
            self.settings.log_level,
            self.settings.logs_root / 'interactive-session.log',
        )
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._prune_expired_verify_evidence()
        self._reap_settled_sessions()

    @property
    def sessions_root(self) -> Path:
        return self.settings.spool_root / 'interactive_sessions'

    @property
    def active_session_pointer_path(self) -> Path:
        return self.sessions_root / 'active_session.json'

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_root / session_id

    def state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / 'session_state.json'

    def events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / 'session_events.jsonl'

    def operator_status_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / 'operator_status.txt'

    def verify_evidence_retention_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / 'verify_evidence_retention.json'

    @property
    def retention_ledger_path(self) -> Path:
        return self.sessions_root / 'retention-ledger.json'

    def _session_mutation_lock(self, session_id: str):
        """Serialize one session's state, events, and projections."""

        # A few focused unit tests construct a lightweight manager double with
        # ``object.__new__`` and replace persistence methods.  There is no
        # filesystem-backed manager state to lock in that deliberately
        # in-memory shape; real instances always initialize ``settings``.
        if not hasattr(self, 'settings'):
            return nullcontext()
        return path_lock(self.state_path(session_id).with_name('.session-mutation'))

    def _active_mutation_lock(self):
        """Serialize active-session admission across manager processes."""

        return path_lock(self.sessions_root / '.active-session-mutation')

    def _strict_generation(self, value: Any, *, default: int = 0) -> int:
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InteractiveSessionError('Interactive session state generation is invalid.')
        return int(value)

    def _write_operator_status(self, session_id: str, text: str) -> None:
        path = self.operator_status_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
        try:
            temporary.write_text(text + '\n', encoding='utf-8', newline='\n')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def verify_evidence_retention_days(self) -> int:
        try:
            return max(int(getattr(self.settings, 'retention_days', 7)), 1)
        except (TypeError, ValueError):
            return 7

    def _verify_evidence_retention_anchor(self, session: dict[str, Any]) -> tuple[str | None, datetime | None]:
        for key in ('closed_at', 'updated_at', 'created_at'):
            parsed = _parse_iso_datetime(session.get(key))
            if parsed is not None:
                return key, parsed
        return None, None

    def _build_verify_evidence_retention_policy(self, session: dict[str, Any]) -> dict[str, Any]:
        session_state = str(session.get('state') or 'unknown')
        anchor_field = None
        anchor_at = None
        expires_at_dt = None
        cleanup_state = 'active_session_exempt'
        if session_state in TERMINAL_SESSION_STATES:
            anchor_field, anchor_at = self._verify_evidence_retention_anchor(session)
            if anchor_at is not None:
                expires_at_dt = anchor_at + timedelta(days=self.verify_evidence_retention_days)
                cleanup_state = 'eligible_when_expired'
            else:
                cleanup_state = 'terminal_session_missing_anchor'
        return {
            'schema_version': VERIFY_EVIDENCE_RETENTION_SCHEMA_VERSION,
            'cleanup_mode': 'delete_frozen_verify_evidence_after_terminal_session_ttl',
            'retention_days': self.verify_evidence_retention_days,
            'retention_anchor': 'session_closed_at',
            'effective_anchor_field': anchor_field,
            'effective_anchor_at': _datetime_to_iso(anchor_at) if anchor_at is not None else None,
            'expires_at': _datetime_to_iso(expires_at_dt) if expires_at_dt is not None else None,
            'session_state': session_state,
            'active_session_exempt': session_state not in TERMINAL_SESSION_STATES,
            'cleanup_state': cleanup_state,
        }

    def _load_verify_evidence_retention_ledger(self, session_id: str) -> dict[str, Any]:
        ledger_path = self.verify_evidence_retention_path(session_id)
        payload = _json_load(ledger_path) or {}
        entries = payload.get('pruned_entries') if isinstance(payload.get('pruned_entries'), list) else []
        return {
            'schema_version': VERIFY_EVIDENCE_RETENTION_LEDGER_SCHEMA_VERSION,
            'policy': {
                'schema_version': VERIFY_EVIDENCE_RETENTION_SCHEMA_VERSION,
                'cleanup_mode': 'delete_frozen_verify_evidence_after_terminal_session_ttl',
                'retention_days': self.verify_evidence_retention_days,
                'retention_anchor': 'session_closed_at',
            },
            'last_swept_at': payload.get('last_swept_at'),
            'pruned_entries': [item for item in entries if isinstance(item, dict)][-VERIFY_EVIDENCE_RETENTION_LEDGER_MAX_ENTRIES:],
        }

    def _save_verify_evidence_retention_ledger(
        self,
        session_id: str,
        *,
        entries: list[dict[str, Any]],
        swept_at: str,
    ) -> None:
        ledger_path = self.verify_evidence_retention_path(session_id)

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            existing = current.get('pruned_entries') if isinstance(current.get('pruned_entries'), list) else []
            combined = [item for item in existing + entries if isinstance(item, dict)]
            deduplicated: list[dict[str, Any]] = []
            seen: set[tuple[str, str, str]] = set()
            for item in combined:
                key = (
                    str(item.get('step') or ''),
                    str(item.get('recorded_at') or ''),
                    str(item.get('expired_at') or ''),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduplicated.append(item)
            return {
                'schema_version': VERIFY_EVIDENCE_RETENTION_LEDGER_SCHEMA_VERSION,
                'policy': {
                    'schema_version': VERIFY_EVIDENCE_RETENTION_SCHEMA_VERSION,
                    'cleanup_mode': 'delete_frozen_verify_evidence_after_terminal_session_ttl',
                    'retention_days': self.verify_evidence_retention_days,
                    'retention_anchor': 'session_closed_at',
                },
                'last_swept_at': swept_at,
                'pruned_entries': deduplicated[-VERIFY_EVIDENCE_RETENTION_LEDGER_MAX_ENTRIES:],
            }

        update_json_object(
            ledger_path,
            merge,
            default={
                'schema_version': VERIFY_EVIDENCE_RETENTION_LEDGER_SCHEMA_VERSION,
                'pruned_entries': [],
            },
        )

    def _merge_retention_ledger_to_central(self, session_id: str) -> None:
        """Keep bounded expiry history without retaining a whole session dir."""

        per_session = self._load_verify_evidence_retention_ledger(session_id)
        entries = per_session.get('pruned_entries') if isinstance(per_session.get('pruned_entries'), list) else []
        if not entries:
            return
        def merge(central: dict[str, Any]) -> dict[str, Any]:
            existing = central.get('entries') if isinstance(central.get('entries'), list) else []
            normalized_existing: list[dict[str, Any]] = []
            seen_keys: set[tuple[str, str, str, str]] = set()

            def add_entry(raw_entry: Any, *, default_session_id: str | None = None) -> None:
                if not isinstance(raw_entry, dict):
                    return
                bounded = _bounded_history_value(raw_entry)
                if not isinstance(bounded, dict):
                    return
                if default_session_id is not None:
                    bounded = {'session_id': default_session_id, **bounded}
                bounded = {
                    key: bounded.get(key)
                    for key in (
                        'session_id', 'step', 'recorded_at', 'pruned_at', 'expired_at',
                        'reason', 'anchor_field', 'anchor_at',
                    )
                    if bounded.get(key) not in (None, '')
                }
                entry_key = (
                    str(bounded.get('session_id') or ''),
                    str(bounded.get('step') or ''),
                    str(bounded.get('recorded_at') or ''),
                    str(bounded.get('expired_at') or ''),
                )
                if entry_key not in seen_keys:
                    seen_keys.add(entry_key)
                    normalized_existing.append(bounded)

            for raw_entry in existing:
                add_entry(raw_entry)
            for entry in entries:
                add_entry(entry, default_session_id=session_id)
            return {
                'schema_version': VERIFY_EVIDENCE_RETENTION_LEDGER_SCHEMA_VERSION,
                'entries': normalized_existing[-VERIFY_EVIDENCE_RETENTION_LEDGER_MAX_ENTRIES:],
                'updated_at': utc_now_iso(),
            }

        update_json_object(
            self.retention_ledger_path,
            merge,
            default={
                'schema_version': VERIFY_EVIDENCE_RETENTION_LEDGER_SCHEMA_VERSION,
                'entries': [],
            },
        )

    def _find_pruned_verify_evidence_entry(self, session_id: str, *, step_name: str, recorded_at: str) -> dict[str, Any] | None:
        ledger = self._load_verify_evidence_retention_ledger(session_id)
        for entry in reversed(ledger.get('pruned_entries') or []):
            if not isinstance(entry, dict):
                continue
            if entry.get('step') == step_name and entry.get('recorded_at') == recorded_at:
                return entry
        return None

    def _prune_expired_verify_evidence(self) -> None:
        sweep_started_at = utc_now_iso()
        sweep_now = _parse_iso_datetime(sweep_started_at)
        if sweep_now is None or not self.sessions_root.exists():
            return

        for session_dir in self.sessions_root.iterdir():
            if not session_dir.is_dir():
                continue

            session_id = session_dir.name
            with self._session_mutation_lock(session_id):
                # Re-read the session under the same lock used by verify and
                # close mutations. A stale pre-lock terminal read must never
                # authorize deletion from a session that became active again.
                session = _json_load(self.state_path(session_id))
                if not isinstance(session, dict):
                    continue
                if str(session.get('state') or '') not in TERMINAL_SESSION_STATES:
                    continue

                anchor_field, anchor_at = self._verify_evidence_retention_anchor(session)
                if anchor_at is None:
                    continue
                expires_at = anchor_at + timedelta(days=self.verify_evidence_retention_days)
                if expires_at > sweep_now:
                    continue

                verify_root = session_dir / 'verify_evidence'
                if not verify_root.exists():
                    continue

                ledger = self._load_verify_evidence_retention_ledger(session_id)
                pruned_entries = list(ledger.get('pruned_entries') or [])
                pruned_count = 0
                for step_dir in verify_root.iterdir():
                    if not step_dir.is_dir():
                        continue
                    for artifact_dir in step_dir.iterdir():
                        if not artifact_dir.is_dir():
                            continue
                        try:
                            shutil.rmtree(artifact_dir)
                        except OSError as exc:
                            self.logger.warning(
                                'interactive verify evidence prune failed session=%s step=%s recorded_at=%s error=%s',
                                session_id,
                                step_dir.name,
                                artifact_dir.name,
                                exc,
                            )
                            continue
                        pruned_entries.append(
                            {
                                'step': step_dir.name,
                                'recorded_at': artifact_dir.name,
                                'pruned_at': sweep_started_at,
                                'expired_at': _datetime_to_iso(expires_at),
                                'reason': 'terminal_session_expired',
                                'anchor_field': anchor_field,
                                'anchor_at': _datetime_to_iso(anchor_at),
                            }
                        )
                        pruned_count += 1
                    if step_dir.exists() and not any(step_dir.iterdir()):
                        step_dir.rmdir()
                if verify_root.exists() and not any(verify_root.iterdir()):
                    verify_root.rmdir()
                if pruned_count:
                    self._save_verify_evidence_retention_ledger(
                        session_id,
                        entries=pruned_entries,
                        swept_at=sweep_started_at,
                    )
                    self.logger.info(
                        'interactive verify evidence prune session=%s count=%s retention_days=%s anchor=%s expired_at=%s',
                        session_id,
                        pruned_count,
                        self.verify_evidence_retention_days,
                        anchor_field,
                        _datetime_to_iso(expires_at),
                    )

    def _has_reconcilable_runtime(self, session: dict[str, Any]) -> bool:
        if str(session.get('state') or '') == 'timed_out_pending_reconciliation':
            return True
        live_runtime = session.get('live_runtime') if isinstance(session.get('live_runtime'), dict) else {}
        if bool(live_runtime.get('reconciliation_pending')):
            return True
        pending = live_runtime.get('pending_command')
        if isinstance(pending, dict) and pending.get('command_id') and not pending.get('reconciled'):
            return True
        metadata = session.get('metadata') if isinstance(session.get('metadata'), dict) else {}
        local_cli = metadata.get('local_cli_v1') if isinstance(metadata.get('local_cli_v1'), dict) else {}
        if bool(local_cli.get('reconciliation_pending')):
            return True
        pending_id = local_cli.get('pending_command_id')
        return bool(pending_id and not local_cli.get('reconciled'))

    def _reap_settled_sessions(self, *, max_settled_sessions: int = MAX_SETTLED_INTERACTIVE_SESSIONS) -> int:
        """Bound terminal session directories without deleting evidence ledgers."""

        if max_settled_sessions < 0 or not self.sessions_root.exists():
            return 0
        active_session_id = self._read_active_session_id()
        settled: list[tuple[datetime, Path]] = []
        for session_dir in self.sessions_root.iterdir():
            if not session_dir.is_dir() or session_dir.is_symlink() or session_dir.name == active_session_id:
                continue
            session = _json_load(self.state_path(session_dir.name))
            if not isinstance(session, dict) or str(session.get('state') or '') not in TERMINAL_SESSION_STATES:
                continue
            if self._has_reconcilable_runtime(session):
                continue
            anchor = (
                _parse_iso_datetime(session.get('closed_at'))
                or _parse_iso_datetime(session.get('updated_at'))
                or _parse_iso_datetime(session.get('created_at'))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            settled.append((anchor, session_dir))
        settled.sort(key=lambda item: (item[0], item[1].name))
        excess = max(0, len(settled) - max_settled_sessions)
        reaped = 0
        for _anchor, session_dir in settled[:excess]:
            try:
                if self.verify_evidence_retention_path(session_dir.name).exists():
                    self._merge_retention_ledger_to_central(session_dir.name)
                shutil.rmtree(session_dir)
            except OSError as exc:
                self.logger.warning('interactive settled session reap failed session=%s error=%s', session_dir.name, exc)
                continue
            reaped += 1
        return reaped

    def _read_active_session_id(self) -> str | None:
        payload = _json_load(self.active_session_pointer_path)
        if not isinstance(payload, dict):
            return None
        session_id = payload.get('session_id')
        return str(session_id) if isinstance(session_id, str) and session_id.strip() else None

    def _write_active_session_id(self, session_id: str | None) -> None:
        if not session_id:
            if self.active_session_pointer_path.exists():
                self.active_session_pointer_path.unlink()
            return
        _json_dump(
            self.active_session_pointer_path,
            {
                'session_id': session_id,
                'updated_at': utc_now_iso(),
            },
        )

    def _load_session(self, session_id: str) -> dict[str, Any] | None:
        return _json_load(self.state_path(session_id))

    def _require_session(self, session_id: str | None = None) -> dict[str, Any]:
        resolved_session_id = session_id or self._read_active_session_id()
        if not resolved_session_id:
            raise InteractiveSessionError('No interactive session is currently open.')
        session = self._load_session(resolved_session_id)
        if not isinstance(session, dict):
            raise InteractiveSessionError(f'Interactive session not found: {resolved_session_id}')
        return session

    def _default_popup_status(self) -> dict[str, Any]:
        return {
            'state': 'not_reported',
            'security_module_name': self.settings.security_module_name,
            'security_module_dll': self.settings.security_module_dll,
            'popup_detected': None,
            'summary': 'Popup/security-module status has not been reported yet.',
            'detail': 'The MVP exposes these fields immediately so logs/TUI clients can see blocking popup state once live probes are wired.',
            'updated_at': utc_now_iso(),
        }

    def _runtime_status(self) -> dict[str, Any]:
        snapshot = load_runtime_readiness_snapshot()
        if snapshot is None:
            return {
                'ok': False,
                'ready': False,
                'status': 'unavailable',
                'summary': build_plain_readiness_failure('interactive session'),
                'worker_name': self.settings.worker_name,
                'checked_at': utc_now_iso(),
            }
        admitted = readiness_matches_current_worker(snapshot)
        return {
            'ok': bool(snapshot.get('ok', True)) and admitted,
            'ready': bool(snapshot.get('ready')) and admitted,
            'status': (
                str(snapshot.get('status') or 'ready')
                if admitted
                else 'not_ready'
            ),
            'summary': (
                str(snapshot.get('summary') or snapshot.get('detail') or '')
                if admitted
                else 'Runtime readiness admission failed; the worker lease or candidate binding is not current.'
            ),
            'worker_name': str(snapshot.get('worker_name') or self.settings.worker_name),
            'artifact_path': snapshot.get('artifact_path'),
            'checked_at': snapshot.get('checked_at') or snapshot.get('updated_at') or utc_now_iso(),
            'checks': snapshot.get('checks') if isinstance(snapshot.get('checks'), dict) else {},
        }

    def _observation_status(self) -> dict[str, Any]:
        viewer = load_viewer_session() or ensure_viewer_session()
        return {
            'ok': True,
            'viewer_session_id': viewer.get('viewer_session_id'),
            'viewer_url': viewer.get('viewer_url'),
            'stream_url': viewer.get('stream_url'),
            'latest_frame_url': viewer.get('latest_frame_url'),
            'updated_at': viewer.get('updated_at'),
            'last_frame': viewer.get('last_frame') if isinstance(viewer.get('last_frame'), dict) else None,
        }

    def _merge_popup_status(self, current: Any, update: Any) -> dict[str, Any]:
        popup = dict(current) if isinstance(current, dict) else self._default_popup_status()
        if not isinstance(update, dict):
            return popup
        for key in ('state', 'security_module_name', 'security_module_dll', 'popup_detected', 'summary', 'detail'):
            value = update.get(key)
            if value is not None:
                popup[key] = value
        popup['updated_at'] = utc_now_iso()
        return popup

    def _normalize_failure_reason(self, failure_reason: Any, *, command: str | None = None, summary: str | None = None) -> dict[str, Any] | None:
        if failure_reason is None:
            return None
        if isinstance(failure_reason, str):
            return {
                'code': 'interactive_failure',
                'command': command,
                'message': _sanitize_history_text(failure_reason),
                'detail': None,
                'updated_at': utc_now_iso(),
            }
        if not isinstance(failure_reason, dict):
            return {
                'code': 'interactive_failure',
                'command': command,
                'message': _sanitize_history_text(failure_reason),
                'detail': None,
                'updated_at': utc_now_iso(),
            }
        payload = _bounded_history_value(dict(failure_reason))
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault('code', 'interactive_failure')
        payload.setdefault('command', command)
        payload.setdefault('message', _sanitize_history_text(summary or 'Interactive command failed.'))
        payload['updated_at'] = utc_now_iso()
        return payload

    def _step_evidence_dir(self, session_id: str, step_name: str, recorded_at: str) -> Path:
        return self.session_dir(session_id) / 'verify_evidence' / step_name / self._require_verify_evidence_token(
            recorded_at,
            label='recorded_at',
        )

    def _require_verify_evidence_token(self, value: str, *, label: str) -> str:
        token = str(value or '').strip()
        if not token or re.fullmatch(r'[A-Za-z0-9_-]+', token) is None:
            raise InteractiveSessionError(f'Invalid verify evidence {label}: {value!r}')
        return token

    def build_verify_evidence_urls(self, *, session_id: str, step_name: str, recorded_at: str) -> dict[str, str]:
        safe_session_id = self._require_verify_evidence_token(session_id, label='session_id')
        safe_step_name = self._require_verify_evidence_token(step_name, label='step')
        evidence_slug = self._require_verify_evidence_token(_timestamp_slug(recorded_at), label='recorded_at')
        base_url = _api_base_url(self.settings)
        artifact_base_url = f'{base_url}/interactive/session/{safe_session_id}/verify-evidence/{safe_step_name}/{evidence_slug}'
        return {
            'artifact_url': artifact_base_url,
            'frame_url': f'{artifact_base_url}/frame',
            'metadata_url': f'{artifact_base_url}/frame.json',
        }

    def load_verify_evidence_artifact(self, *, session_id: str, step_name: str, recorded_at: str) -> dict[str, Any]:
        safe_session_id = self._require_verify_evidence_token(session_id, label='session_id')
        safe_step_name = self._require_verify_evidence_token(step_name, label='step')
        evidence_slug = self._require_verify_evidence_token(_timestamp_slug(recorded_at), label='recorded_at')
        evidence_dir = self._step_evidence_dir(safe_session_id, safe_step_name, evidence_slug)
        metadata_path = evidence_dir / f'{safe_step_name}-frame.json'
        if not metadata_path.exists():
            pruned_entry = self._find_pruned_verify_evidence_entry(
                safe_session_id,
                step_name=safe_step_name,
                recorded_at=evidence_slug,
            )
            if isinstance(pruned_entry, dict):
                raise InteractiveSessionError(
                    'Frozen verify evidence expired and was pruned by retention policy: '
                    f'session={safe_session_id} step={safe_step_name} recorded_at={evidence_slug} '
                    f"pruned_at={pruned_entry.get('pruned_at') or 'unknown'}"
                )
            raise InteractiveSessionError(
                f'Frozen verify evidence not found: session={safe_session_id} step={safe_step_name} recorded_at={evidence_slug}'
            )

        if metadata_path.is_symlink():
            raise InteractiveSessionError('Frozen verify evidence metadata is a symlink.')
        payload = _json_load(metadata_path) or {}
        metadata_hash = payload.get('metadata_sha256')
        if not isinstance(metadata_hash, str) or metadata_hash != _canonical_payload_sha256(
            {key: value for key, value in payload.items() if key != 'metadata_sha256'}
        ):
            raise InteractiveSessionError('Frozen verify evidence metadata hash did not match readback bytes.')
        session = self._load_session(safe_session_id) or {}
        retention = dict(payload.get('retention')) if isinstance(payload.get('retention'), dict) else {}
        if isinstance(session, dict) and session:
            retention.update(self._build_verify_evidence_retention_policy(session))
        if retention:
            retention['retention_ledger_path'] = str(self.verify_evidence_retention_path(safe_session_id))
            payload['retention'] = retention
        frame_payload = payload.get('frame') if isinstance(payload.get('frame'), dict) else {}
        frame_path = _existing_file(frame_payload.get('step_bound_frame_path'))
        if frame_path is not None:
            if frame_path.is_symlink() or frame_path.parent != evidence_dir:
                raise InteractiveSessionError('Frozen verify evidence frame escaped its step-bound directory.')
            frame_size, frame_sha256 = _sha256_file(frame_path)
            if frame_size != int(frame_payload.get('step_bound_frame_size_bytes') or -1) or frame_sha256 != str(
                frame_payload.get('step_bound_frame_sha256') or ''
            ):
                raise InteractiveSessionError('Frozen verify evidence frame hash did not match readback bytes.')
        urls = self.build_verify_evidence_urls(
            session_id=safe_session_id,
            step_name=safe_step_name,
            recorded_at=evidence_slug,
        )
        return {
            'session_id': safe_session_id,
            'step_name': safe_step_name,
            'recorded_at': evidence_slug,
            'evidence_dir': evidence_dir,
            'metadata_path': metadata_path,
            'frame_path': frame_path,
            'payload': payload,
            'urls': urls,
        }

    def _resolve_verify_frame_image_source(self, gui_payload: dict[str, Any], frame_payload: dict[str, Any]) -> Path | None:
        artifacts = gui_payload.get('artifacts') if isinstance(gui_payload.get('artifacts'), dict) else {}
        primary_image = gui_payload.get('primary_image') if isinstance(gui_payload.get('primary_image'), dict) else {}
        for candidate in (
            primary_image.get('path'),
            frame_payload.get('frame_path'),
            artifacts.get('latest_frame_path'),
            str(latest_frame_path()) if latest_frame_path().exists() else None,
        ):
            resolved = _existing_file(candidate)
            if resolved is not None:
                return resolved
        return None

    def _resolve_verify_frame_metadata_source(self, gui_payload: dict[str, Any]) -> Path | None:
        artifacts = gui_payload.get('artifacts') if isinstance(gui_payload.get('artifacts'), dict) else {}
        for candidate in (
            artifacts.get('latest_frame_metadata_path'),
            str(latest_frame_metadata_path()) if latest_frame_metadata_path().exists() else None,
        ):
            resolved = _existing_file(candidate)
            if resolved is not None:
                return resolved
        return None

    def _bind_verify_step_gui_evidence(
        self,
        session: dict[str, Any],
        *,
        step_name: str,
        verify_result: dict[str, Any],
        recorded_at: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        enriched = dict(verify_result)
        gui_payload = dict(enriched.get('gui')) if isinstance(enriched.get('gui'), dict) else {}
        frame_payload = dict(gui_payload.get('frame')) if isinstance(gui_payload.get('frame'), dict) else {}
        observation_status = dict(gui_payload.get('observation_status')) if isinstance(gui_payload.get('observation_status'), dict) else {}
        viewer_payload = dict(gui_payload.get('viewer')) if isinstance(gui_payload.get('viewer'), dict) else {}
        artifacts = dict(gui_payload.get('artifacts')) if isinstance(gui_payload.get('artifacts'), dict) else {}
        primary_image = dict(gui_payload.get('primary_image')) if isinstance(gui_payload.get('primary_image'), dict) else {}

        source_image_path = self._resolve_verify_frame_image_source(gui_payload, frame_payload)
        source_metadata_path = self._resolve_verify_frame_metadata_source(gui_payload)
        evidence_slug = f'{_timestamp_slug(recorded_at)}-{uuid.uuid4().hex}'
        evidence_dir = self._step_evidence_dir(str(session['session_id']), step_name, evidence_slug)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_urls = self.build_verify_evidence_urls(
            session_id=str(session['session_id']),
            step_name=step_name,
            recorded_at=evidence_slug,
        )
        retention = self._build_verify_evidence_retention_policy(session)

        bound_frame_path: Path | None = None
        if source_image_path is not None:
            suffix = source_image_path.suffix or '.png'
            bound_frame_path = evidence_dir / f'{step_name}-frame{suffix}'
            shutil.copyfile(source_image_path, bound_frame_path)
            frame_size, frame_sha256 = _sha256_file(bound_frame_path)
        else:
            frame_size, frame_sha256 = 0, None

        captured_at = frame_payload.get('captured_at')
        recorded_at_dt = _parse_iso_datetime(recorded_at)
        captured_at_dt = _parse_iso_datetime(captured_at)
        frame_age_ms, capture_relation = resolve_verify_capture_timing(
            recorded_at=recorded_at_dt,
            captured_at=captured_at_dt,
        )

        # Keep the manager responsible for filesystem persistence and session wiring,
        # while the verify-evidence module owns freshness/trust interpretation.
        binding, policy = build_verify_step_binding(
            step_name=step_name,
            recorded_at=recorded_at,
            captured_at=captured_at,
            source_image_path=str(source_image_path) if source_image_path is not None else None,
            source_metadata_path=str(source_metadata_path) if source_metadata_path is not None else None,
            observation_status=observation_status.get('observation_status'),
            observation_reason_code=observation_status.get('reason_code'),
            image_frozen=bound_frame_path is not None,
            frame_age_ms=frame_age_ms,
            capture_relation=capture_relation,
            soft_warn_age_ms=VERIFY_EVIDENCE_SOFT_WARN_AGE_MS,
            hard_stale_age_ms=VERIFY_EVIDENCE_HARD_STALE_AGE_MS,
        )

        frame_payload['step_binding'] = binding
        frame_payload['step_bound_at'] = recorded_at
        if bound_frame_path is not None:
            frame_payload['step_bound_frame_path'] = str(bound_frame_path)
            frame_payload['step_bound_frame_url'] = evidence_urls['frame_url']
        frame_payload['step_bound_frame_metadata_url'] = evidence_urls['metadata_url']
        if bound_frame_path is not None:
            frame_payload['step_bound_frame_size_bytes'] = frame_size
            frame_payload['step_bound_frame_sha256'] = frame_sha256

        if bound_frame_path is not None:
            primary_image.update(
                {
                    'kind': 'hancom_gui_verify_step_bound_frame',
                    'label': f'Hancom GUI {step_name} step-bound frame',
                    'path': str(bound_frame_path),
                    # The primary image now points at a dedicated frozen per-step HTTP route,
                    # not the mutable viewer URL, so later viewer refreshes cannot silently
                    # swap the evidence underneath a verify result.
                    'url': evidence_urls['frame_url'],
                    'captured_at': captured_at,
                    'ok': frame_payload.get('ok'),
                    'reason_code': frame_payload.get('reason_code'),
                    'window_handle': frame_payload.get('window_handle'),
                    'window_pid': frame_payload.get('window_pid'),
                    'window_title': frame_payload.get('window_title'),
                    'window_class': frame_payload.get('window_class'),
                }
            )

        step_binding_payload = {
            'ok': bound_frame_path is not None or bool(frame_payload),
            'session_id': session.get('session_id'),
            'step': step_name,
            'recorded_at': recorded_at,
            'artifact_id': evidence_slug,
            'retention': retention,
            'binding': binding,
            'frame': frame_payload,
            'observation_status': observation_status,
            'viewer': viewer_payload,
        }
        if source_metadata_path is not None and source_metadata_path.exists():
            try:
                step_binding_payload['source_frame_metadata'] = json.loads(source_metadata_path.read_text(encoding='utf-8'))
            except Exception:
                step_binding_payload['source_frame_metadata_error'] = f'failed_to_read:{source_metadata_path}'

        step_metadata_path = evidence_dir / f'{step_name}-frame.json'
        step_binding_payload['metadata_sha256'] = _canonical_payload_sha256(step_binding_payload)
        _json_dump(step_metadata_path, step_binding_payload)

        artifacts.update(
            {
                'step_bound_frame_path': str(bound_frame_path) if bound_frame_path is not None else None,
                'step_bound_frame_url': evidence_urls['frame_url'] if bound_frame_path is not None else None,
                'step_bound_frame_metadata_path': str(step_metadata_path),
                'step_bound_frame_metadata_url': evidence_urls['metadata_url'],
                'step_bound_evidence_dir': str(evidence_dir),
                'step_bound_evidence_url': evidence_urls['artifact_url'],
                'verify_evidence_retention_path': str(self.verify_evidence_retention_path(str(session['session_id']))),
            }
        )
        gui_payload['artifacts'] = artifacts
        gui_payload['primary_image'] = primary_image or None
        gui_payload['images'] = [primary_image] if primary_image else []
        gui_payload['frame'] = frame_payload
        gui_payload['viewer'] = viewer_payload
        gui_payload['observation_status'] = observation_status
        gui_payload['step_binding'] = binding
        gui_payload['freshness_policy'] = policy['freshness']
        gui_payload['trust_policy'] = policy['trust']
        gui_payload['warning_codes'] = policy['warning_codes']
        gui_payload['policy_summary'] = policy['summary']
        gui_payload['source'] = 'verify-step.step-bound-frozen-frame'
        gui_payload['capture_state'] = 'bound' if bound_frame_path is not None else 'metadata-only'
        gui_payload['updated_at'] = recorded_at
        gui_payload['artifact_url'] = evidence_urls['artifact_url']
        enriched['gui'] = gui_payload
        return enriched, {
            f'{step_name}_frame_path': str(bound_frame_path) if bound_frame_path is not None else '',
            f'{step_name}_frame_metadata_path': str(step_metadata_path),
            f'{step_name}_frame_url': evidence_urls['frame_url'] if bound_frame_path is not None else '',
            f'{step_name}_frame_metadata_url': evidence_urls['metadata_url'],
            f'{step_name}_evidence_url': evidence_urls['artifact_url'],
        }

    def _merge_mapping(self, current: Any, update: Any) -> dict[str, Any]:
        merged = dict(current) if isinstance(current, dict) else {}
        if isinstance(update, dict):
            merged.update(update)
        return merged

    def _save_session_locked(
        self,
        session: dict[str, Any],
        *,
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        current_generation = self._strict_generation(
            current.get('state_generation') if isinstance(current, dict) else None,
        )
        if isinstance(current, dict) and 'state_generation' in session:
            requested_generation = self._strict_generation(session.get('state_generation'))
            if requested_generation != current_generation:
                raise InteractiveSessionError(
                    'Interactive session state changed while the command was being prepared.'
                )
        now_iso = utc_now_iso()
        session['updated_at'] = now_iso
        session['readiness'] = self._runtime_status()
        session['observation'] = self._observation_status()
        session['popup_status'] = self._merge_popup_status(session.get('popup_status'), None)
        progress, lines, text = render_operator_status(
            session,
            progress_command_sequence=PROGRESS_COMMAND_SEQUENCE,
            terminal_session_states=TERMINAL_SESSION_STATES,
            now_iso=now_iso,
        )
        session['command_progress'] = progress
        session['operator_status_lines'] = lines
        session['operator_status_text'] = text
        session['state_generation'] = current_generation + 1
        _json_dump(self.state_path(str(session['session_id'])), session)
        self._write_operator_status(str(session['session_id']), text)
        return session

    def _save_session(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session.get('session_id') or '').strip()
        if not session_id:
            raise InteractiveSessionError('Interactive session is missing session_id.')
        with self._session_mutation_lock(session_id):
            current = self._load_session(session_id)
            if not isinstance(current, dict):
                current = None
            return self._save_session_locked(session, current=current)

    def _session_transaction(
        self,
        session_id: str,
        mutator: Any,
        *,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._session_mutation_lock(session_id):
            current = self._load_session(session_id)
            if not isinstance(current, dict):
                raise InteractiveSessionError(f'Interactive session not found: {session_id}')
            updated = mutator(copy.deepcopy(current))
            if not isinstance(updated, dict):
                raise InteractiveSessionError('Interactive session mutation must return an object.')
            persisted = self._save_session_locked(updated, current=current)
            if event is not None:
                self._append_event(session_id, event)
            return persisted

    def _append_event(self, session_id: str, payload: dict[str, Any]) -> None:
        event = dict(payload)
        event.setdefault('recorded_at', utc_now_iso())
        _append_jsonl(self.events_path(session_id), event)

    def _log_operator_event(self, session: dict[str, Any], *, command: str, state: str, summary: str | None = None) -> None:
        popup = session.get('popup_status') if isinstance(session.get('popup_status'), dict) else {}
        failure = session.get('failure_reason') if isinstance(session.get('failure_reason'), dict) else {}
        verify_pre = session.get('verify_pre') if isinstance(session.get('verify_pre'), dict) else {}
        verify_post = session.get('verify_post') if isinstance(session.get('verify_post'), dict) else {}
        message = (
            'interactive session=%s state=%s command=%s result=%s '
            'verify_pre=%s verify_post=%s popup=%s failure=%s summary=%s'
        )
        log_args = (
            session.get('session_id'),
            session.get('state'),
            command,
            state,
            verify_pre.get('state', 'pending'),
            verify_post.get('state', 'pending'),
            popup.get('state', 'unknown'),
            failure.get('message') or failure.get('code') or 'none',
            summary or '-',
        )
        if state == 'failed':
            self.logger.error(message, *log_args)
        else:
            self.logger.info(message, *log_args)

    def open_session(
        self,
        *,
        source_path: Path,
        source_filename: str,
        file_size_bytes: int,
        content_type: str | None,
        session_label: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self._prune_expired_verify_evidence()
        self._reap_settled_sessions()
        session_id = str(session_id or uuid.uuid4().hex).strip()
        if not session_id:
            raise InteractiveSessionError('Interactive session id must not be empty.')
        now = utc_now_iso()
        session = {
            'session_id': session_id,
            'state': 'open',
            'workflow_mode': 'interactive',
            'runtime_lane': '.51',
            'source_path': str(source_path),
            'source_filename': source_filename,
            'file_size_bytes': int(file_size_bytes),
            'content_type': content_type,
            'session_label': session_label,
            'created_at': now,
            'updated_at': now,
            'closed_at': None,
            'current_command': 'open',
            'command_progress': {},
            'command_history': [
                {
                    'recorded_at': now,
                    'command': 'open',
                    'state': 'succeeded',
                    'summary': 'Interactive session opened.',
                }
            ],
            'find_result': {},
            'choose_result': {},
            'selected_candidate': None,
            'active_target': {},
            'runtime_preparation': {},
            'lock_status': {},
            'verify_pre': {
                'state': 'pending',
                'verification_mode': None,
                'summary': None,
                'result': {},
                'gui': {},
                'updated_at': None,
            },
            'apply_result': {},
            'verify_post': {
                'state': 'pending',
                'verification_mode': None,
                'summary': None,
                'result': {},
                'gui': {},
                'updated_at': None,
            },
            'undo_result': {},
            'popup_status': self._default_popup_status(),
            'failure_reason': None,
            'readiness': {},
            'observation': {},
            'live_runtime': {},
            'operator_status_lines': [],
            'operator_status_text': '',
            'artifacts': {
                'session_state_path': str(self.state_path(session_id)),
                'session_events_path': str(self.events_path(session_id)),
                'operator_status_path': str(self.operator_status_path(session_id)),
                'verify_evidence_retention_path': str(self.verify_evidence_retention_path(session_id)),
            },
            'metadata': dict(metadata or {}),
        }
        with self._active_mutation_lock():
            active_session_id = self._read_active_session_id()
            if active_session_id:
                existing = self._load_session(active_session_id)
                if isinstance(existing, dict) and existing.get('state') not in TERMINAL_SESSION_STATES:
                    raise InteractiveSessionError(
                        f'Interactive session already open: {active_session_id}. Close it before opening a new session.'
                    )
            with self._session_mutation_lock(session_id):
                if self.state_path(session_id).exists():
                    raise InteractiveSessionError(f'Interactive session already exists: {session_id}')
                session = self._save_session_locked(session, current=None)
                self._append_event(
                    session_id,
                    {
                        'kind': 'session_opened',
                        'command': 'open',
                        'state': 'succeeded',
                        'summary': 'Interactive session opened.',
                        'source_path': str(source_path),
                    },
                )
            self._write_active_session_id(session_id)
        self._log_operator_event(session, command='open', state='succeeded', summary='Interactive session opened.')
        return session

    def update_session(
        self,
        *,
        session_id: str,
        live_runtime: dict[str, Any] | None = None,
        active_target: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        session_state: str | None = None,
    ) -> dict[str, Any]:
        resolved_session_id = str(self._require_session(session_id)['session_id'])

        def mutate(session: dict[str, Any]) -> dict[str, Any]:
            if isinstance(active_target, dict) and active_target:
                session['active_target'] = active_target
            if isinstance(metadata, dict) and metadata:
                session['metadata'] = self._merge_mapping(session.get('metadata'), metadata)
            if isinstance(artifacts, dict) and artifacts:
                session['artifacts'] = self._merge_mapping(session.get('artifacts'), artifacts)
            if isinstance(live_runtime, dict) and live_runtime:
                session['live_runtime'] = self._merge_mapping(session.get('live_runtime'), live_runtime)
            if session_state is not None:
                session['state'] = session_state
            return session

        return self._session_transaction(resolved_session_id, mutate)

    def get_status(self, session_id: str | None = None) -> dict[str, Any]:
        resolved_session_id = str(self._require_session(session_id)['session_id'])
        return self._session_transaction(resolved_session_id, lambda session: session)

    def record_command(
        self,
        command: str,
        *,
        session_id: str | None = None,
        state: str = 'succeeded',
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
        find_result: dict[str, Any] | None = None,
        choose_result: dict[str, Any] | None = None,
        selected_candidate: dict[str, Any] | None = None,
        active_target: dict[str, Any] | None = None,
        runtime_preparation: dict[str, Any] | None = None,
        lock_status: dict[str, Any] | None = None,
        verify_stage: str | None = None,
        verify_result: dict[str, Any] | None = None,
        apply_result: dict[str, Any] | None = None,
        undo_result: dict[str, Any] | None = None,
        popup_status: dict[str, Any] | None = None,
        failure_reason: dict[str, Any] | str | None = None,
        metadata: dict[str, Any] | None = None,
        live_runtime: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        session_state: str | None = None,
    ) -> dict[str, Any]:
        history_payload = _bounded_command_history_payload(command, payload or {})
        session_hint = self._require_session(session_id)
        resolved_session_id = str(session_hint['session_id'])
        active_lock = self._active_mutation_lock() if command == 'close' else nullcontext()
        with active_lock:
            with self._session_mutation_lock(resolved_session_id):
                session = (
                    self._load_session(resolved_session_id)
                    if hasattr(self, 'settings')
                    else session_hint
                )
                if not isinstance(session, dict):
                    raise InteractiveSessionError(f'Interactive session not found: {resolved_session_id}')
                recorded_at = utc_now_iso()
                session['current_command'] = command

                # Preserve the role split in state so later live command handlers can plug in
                # without collapsing candidate search, cursor entry, verification, and undo into one blob.
                if isinstance(find_result, dict) and find_result:
                    session['find_result'] = find_result
                if isinstance(choose_result, dict) and choose_result:
                    session['choose_result'] = choose_result
                if isinstance(selected_candidate, dict) and selected_candidate:
                    session['selected_candidate'] = selected_candidate
                if isinstance(active_target, dict) and active_target:
                    session['active_target'] = active_target
                if isinstance(runtime_preparation, dict) and runtime_preparation:
                    session['runtime_preparation'] = self._merge_mapping(session.get('runtime_preparation'), runtime_preparation)
                if isinstance(lock_status, dict) and lock_status:
                    session['lock_status'] = lock_status
                if isinstance(apply_result, dict) and apply_result:
                    session['apply_result'] = apply_result
                if isinstance(undo_result, dict) and undo_result:
                    session['undo_result'] = undo_result
                if isinstance(metadata, dict) and metadata:
                    session['metadata'] = self._merge_mapping(session.get('metadata'), metadata)
                if isinstance(live_runtime, dict) and live_runtime:
                    session['live_runtime'] = self._merge_mapping(session.get('live_runtime'), live_runtime)
                if isinstance(artifacts, dict) and artifacts:
                    session['artifacts'] = self._merge_mapping(session.get('artifacts'), artifacts)

                session['popup_status'] = self._merge_popup_status(session.get('popup_status'), popup_status)

                if verify_stage in {'pre', 'post'}:
                    target_key = 'verify_pre' if verify_stage == 'pre' else 'verify_post'
                    existing = session.get(target_key) if isinstance(session.get(target_key), dict) else {}
                    merged = dict(existing)
                    if isinstance(verify_result, dict):
                        verify_result, step_artifacts = self._bind_verify_step_gui_evidence(
                            session,
                            step_name=f'verify-{verify_stage}',
                            verify_result=verify_result,
                            recorded_at=recorded_at,
                        )
                        merged.update(verify_result)
                        session['artifacts'] = self._merge_mapping(session.get('artifacts'), step_artifacts)
                    merged['state'] = state
                    merged.setdefault('summary', summary)
                    merged['updated_at'] = recorded_at
                    session[target_key] = merged

                normalized_failure = self._normalize_failure_reason(failure_reason, command=command, summary=summary)
                if state == 'failed' and normalized_failure is None:
                    normalized_failure = self._normalize_failure_reason({}, command=command, summary=summary)
                if normalized_failure is not None:
                    session['failure_reason'] = normalized_failure
                    session['state'] = 'failed'
                elif session_state is not None:
                    session['state'] = session_state
                elif command == 'close':
                    session['state'] = 'closed'
                elif session.get('state') not in TERMINAL_SESSION_STATES:
                    session['state'] = 'ready'

                if command == 'close':
                    session['closed_at'] = utc_now_iso()

                entry = {
                    'recorded_at': recorded_at,
                    'command': command,
                    'state': state,
                    'summary': _bounded_history_value(summary),
                    'payload': history_payload,
                }
                history = session.get('command_history') if isinstance(session.get('command_history'), list) else []
                history.append(entry)
                session['command_history'] = history[-MAX_INTERACTIVE_COMMAND_HISTORY:]

                if hasattr(self, 'settings'):
                    session = self._save_session_locked(session, current=session)
                else:
                    # Compatibility callers construct an in-memory manager via
                    # object.__new__ and replace _save_session with a test seam.
                    session = self._save_session(session)
                self._append_event(resolved_session_id, entry)

            if command == 'close':
                active_session_id = self._read_active_session_id()
                if active_session_id == resolved_session_id:
                    self._write_active_session_id(None)

        self._log_operator_event(session, command=command, state=state, summary=summary)
        return session
