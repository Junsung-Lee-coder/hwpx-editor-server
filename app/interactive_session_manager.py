from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.interactive.operator_status import render_operator_status
from app.interactive.verify_evidence import build_verify_step_binding, resolve_verify_capture_timing
from app.logging_utils import configure_logger
from app.observation import ensure_viewer_session, latest_frame_metadata_path, latest_frame_path, load_viewer_session
from app.readiness import build_plain_readiness_failure, load_runtime_readiness_snapshot

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


class InteractiveSessionError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _datetime_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _json_dump(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _json_load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    return payload if isinstance(payload, dict) else None


def _append_jsonl(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + '\n')


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
    def verify_evidence_retention_days(self) -> int:
        try:
            return max(int(self.settings.retention_days), 1)
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
        _json_dump(
            self.verify_evidence_retention_path(session_id),
            {
                'schema_version': VERIFY_EVIDENCE_RETENTION_LEDGER_SCHEMA_VERSION,
                'policy': {
                    'schema_version': VERIFY_EVIDENCE_RETENTION_SCHEMA_VERSION,
                    'cleanup_mode': 'delete_frozen_verify_evidence_after_terminal_session_ttl',
                    'retention_days': self.verify_evidence_retention_days,
                    'retention_anchor': 'session_closed_at',
                },
                'last_swept_at': swept_at,
                'pruned_entries': entries[-VERIFY_EVIDENCE_RETENTION_LEDGER_MAX_ENTRIES:],
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
        return {
            'ok': bool(snapshot.get('ok', True)),
            'ready': bool(snapshot.get('ready')),
            'status': str(snapshot.get('status') or ('ready' if snapshot.get('ready') else 'not_ready')),
            'summary': str(snapshot.get('summary') or snapshot.get('detail') or ''),
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
                'message': failure_reason,
                'detail': None,
                'updated_at': utc_now_iso(),
            }
        if not isinstance(failure_reason, dict):
            return {
                'code': 'interactive_failure',
                'command': command,
                'message': str(failure_reason),
                'detail': None,
                'updated_at': utc_now_iso(),
            }
        payload = dict(failure_reason)
        payload.setdefault('code', 'interactive_failure')
        payload.setdefault('command', command)
        payload.setdefault('message', summary or 'Interactive command failed.')
        payload['updated_at'] = utc_now_iso()
        return payload

    def _step_evidence_dir(self, session_id: str, step_name: str, recorded_at: str) -> Path:
        return self.session_dir(session_id) / 'verify_evidence' / step_name / _timestamp_slug(recorded_at)

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

        payload = _json_load(metadata_path) or {}
        session = self._load_session(safe_session_id) or {}
        retention = dict(payload.get('retention')) if isinstance(payload.get('retention'), dict) else {}
        if isinstance(session, dict) and session:
            retention.update(self._build_verify_evidence_retention_policy(session))
        if retention:
            retention['retention_ledger_path'] = str(self.verify_evidence_retention_path(safe_session_id))
            payload['retention'] = retention
        frame_payload = payload.get('frame') if isinstance(payload.get('frame'), dict) else {}
        frame_path = _existing_file(frame_payload.get('step_bound_frame_path'))
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
        evidence_dir = self._step_evidence_dir(str(session['session_id']), step_name, recorded_at)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_urls = self.build_verify_evidence_urls(
            session_id=str(session['session_id']),
            step_name=step_name,
            recorded_at=recorded_at,
        )
        retention = self._build_verify_evidence_retention_policy(session)

        bound_frame_path: Path | None = None
        if source_image_path is not None:
            suffix = source_image_path.suffix or '.png'
            bound_frame_path = evidence_dir / f'{step_name}-frame{suffix}'
            shutil.copyfile(source_image_path, bound_frame_path)

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

    def _save_session(self, session: dict[str, Any]) -> dict[str, Any]:
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
        _json_dump(self.state_path(str(session['session_id'])), session)
        self.operator_status_path(str(session['session_id'])).write_text(text + '\n', encoding='utf-8')
        return session

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
        active_session_id = self._read_active_session_id()
        if active_session_id:
            existing = self._load_session(active_session_id)
            if isinstance(existing, dict) and existing.get('state') not in TERMINAL_SESSION_STATES:
                raise InteractiveSessionError(
                    f'Interactive session already open: {active_session_id}. Close it before opening a new session.'
                )

        session_id = str(session_id or uuid.uuid4().hex).strip()
        if not session_id:
            raise InteractiveSessionError('Interactive session id must not be empty.')
        if self.state_path(session_id).exists():
            raise InteractiveSessionError(f'Interactive session already exists: {session_id}')
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
        self._save_session(session)
        self._write_active_session_id(session_id)
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
        session = self._require_session(session_id)
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
        return self._save_session(session)

    def get_status(self, session_id: str | None = None) -> dict[str, Any]:
        session = self._require_session(session_id)
        return self._save_session(session)

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
        session = self._require_session(session_id)
        resolved_session_id = str(session['session_id'])
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
                payload = verify_result
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
            'summary': summary,
            'payload': payload or {},
        }
        history = session.get('command_history') if isinstance(session.get('command_history'), list) else []
        history.append(entry)
        session['command_history'] = history[-100:]

        self._save_session(session)
        self._append_event(resolved_session_id, entry)
        self._log_operator_event(session, command=command, state=state, summary=summary)

        if command == 'close':
            active_session_id = self._read_active_session_id()
            if active_session_id == resolved_session_id:
                self._write_active_session_id(None)

        return session
