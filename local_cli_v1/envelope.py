from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ENVELOPE_SCHEMA_VERSION = 'local-cli/envelope/v1'


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_envelope(
    *,
    result: str = 'ok',
    where: str,
    how: str,
    changed: str,
    proof: str,
    next_step: str,
    artifact_role: str | None = None,
    artifact_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    manifest_data: dict[str, Any] | None = None,
    warnings: list[str] | tuple[str, ...] | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    """Build the stable local CLI user-facing envelope.

    Human output and JSON output share the same field meanings:
    result/where/how/changed are status strings, proof carries evidence, and
    next carries the operator review instruction.
    """

    clean_result = str(result).strip() or 'ok'
    clean_blocked_reason = _clean_optional_text(blocked_reason)
    if clean_result == 'blocked' and clean_blocked_reason is None:
        raise ValueError('local-cli/envelope/v1 blocked result requires a non-empty blocked_reason')

    clean_warnings = [str(item).strip() for item in (warnings or []) if str(item).strip()]
    artifact = None
    if artifact_role or artifact_path:
        artifact = {
            'role': _clean_optional_text(artifact_role) or 'artifact',
            'path': str(artifact_path) if artifact_path is not None else None,
        }
    manifest = None
    if manifest_path is not None or manifest_data is not None:
        manifest = {
            'path': str(manifest_path) if manifest_path is not None else None,
            'data': manifest_data,
        }
    return {
        'schema_version': ENVELOPE_SCHEMA_VERSION,
        'result': clean_result,
        'where': str(where),
        'how': str(how),
        'changed': str(changed),
        'proof': {
            'summary': str(proof),
            'artifact': artifact,
            'manifest': manifest,
        },
        'next': {
            'review_instruction': str(next_step),
        },
        'warnings': clean_warnings,
        'blocked_reason': clean_blocked_reason,
    }


def _proof_summary(envelope: dict[str, Any]) -> str:
    proof = envelope.get('proof') if isinstance(envelope.get('proof'), dict) else {}
    return str(proof.get('summary') or '')


def format_human_envelope(envelope: dict[str, Any]) -> str:
    """Render the canonical six-line human envelope plus optional structured hints."""

    next_data = envelope.get('next') if isinstance(envelope.get('next'), dict) else {}
    lines = [
        f"result: {envelope.get('result')}",
        f"where: {envelope.get('where')}",
        f"how: {envelope.get('how')}",
        f"changed: {envelope.get('changed')}",
        f"proof: {_proof_summary(envelope)}",
    ]
    proof = envelope.get('proof') if isinstance(envelope.get('proof'), dict) else {}
    manifest = proof.get('manifest') if isinstance(proof.get('manifest'), dict) else None
    if manifest and manifest.get('path'):
        lines.append(f"manifest: {manifest.get('path')}")
    for warning in envelope.get('warnings') or []:
        lines.append(f'warning: {warning}')
    if envelope.get('blocked_reason'):
        lines.append(f"blocked: {envelope.get('blocked_reason')}")
    lines.append(f"next: {next_data.get('review_instruction') or ''}")
    return '\n'.join(lines)


def dumps_envelope_json(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, indent=2)
