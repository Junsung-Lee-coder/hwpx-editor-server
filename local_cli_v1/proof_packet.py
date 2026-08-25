from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .transport import ApiError


class ProofPacketError(ApiError):
    pass


@dataclass(frozen=True)
class ProofArtifactSpec:
    state_key: str
    role: str
    default_suffix: str
    many: bool = False


ARTIFACT_SPECS: tuple[ProofArtifactSpec, ...] = (
    ProofArtifactSpec('last_saved_working_copy_path', 'saved_working_copy', '.hwp'),
    ProofArtifactSpec('last_export_path', 'exported_pdf', '.pdf'),
    ProofArtifactSpec('last_export_manifest_path', 'export_manifest', '.json'),
    ProofArtifactSpec('last_page_screenshot_path', 'rendered_page_proof', '.png'),
    ProofArtifactSpec('last_page_screenshot_manifest_path', 'rendered_page_manifest', '.json'),
    ProofArtifactSpec('last_export_proof_page_paths', 'export_proof_page', '.png', many=True),
    ProofArtifactSpec('last_export_proof_contact_sheet_path', 'export_proof_contact_sheet', '.png'),
    ProofArtifactSpec('last_export_proof_manifest_path', 'export_proof_manifest', '.json'),
    ProofArtifactSpec('last_screenshot_path', 'live_editor_screenshot', '.png'),
)


DELIVERY_SOURCE_ROLES = {'saved_working_copy', 'exported_pdf'}
RENDERED_PROOF_ROLES = {'rendered_page_proof', 'export_proof_page', 'export_proof_contact_sheet'}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_destination(packet_dir: Path, spec: ProofArtifactSpec, source: Path, *, item_index: int | None = None) -> Path:
    suffix = source.suffix or spec.default_suffix
    if spec.many:
        stem = source.stem or f'{spec.role}-{item_index or 1:03d}'
        if stem.startswith('page-'):
            stem = stem[len('page-'):]
        token = stem
        if not token.startswith(spec.role):
            token = f'{spec.role}-{stem}'
        return packet_dir / f'{token}{suffix}'
    return packet_dir / f'{spec.role}{suffix}'


def _copy_artifact(*, source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source.resolve() == destination.resolve():
            return destination
    except FileNotFoundError:
        pass
    shutil.copy2(source, destination)
    return destination


def _iter_state_paths(raw_value: Any, *, many: bool) -> list[str]:
    if many:
        if isinstance(raw_value, list):
            return [str(item) for item in raw_value if isinstance(item, str) and item.strip()]
        if isinstance(raw_value, str) and raw_value.strip():
            return [raw_value]
        return []
    if isinstance(raw_value, str) and raw_value.strip():
        return [raw_value]
    return []


def _artifact_record(*, spec: ProofArtifactSpec, source: Path, destination: Path) -> dict[str, Any]:
    return {
        'role': spec.role,
        'state_key': spec.state_key,
        'source_path': str(source),
        'packet_path': str(destination),
        'relative_path': destination.name,
        'sha256': _sha256_file(destination),
        'bytes': destination.stat().st_size,
    }


def _delivery_ready(artifacts: list[dict[str, Any]]) -> tuple[bool, str]:
    roles = {str(item.get('role') or '') for item in artifacts}
    has_source = bool(roles & DELIVERY_SOURCE_ROLES)
    has_rendered_proof = bool(roles & RENDERED_PROOF_ROLES)
    if has_source and has_rendered_proof:
        return True, 'packet includes a delivery source plus rendered proof artifact'
    if not has_rendered_proof:
        return False, 'packet is missing rendered proof artifact'
    return False, 'packet is missing saved/exported delivery source artifact'


def build_proof_packet(*, out_dir: Path, state: dict[str, Any], state_path: Path | None = None) -> dict[str, Any]:
    packet_dir = out_dir.expanduser()
    packet_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = packet_dir / 'manifest.json'
    artifacts: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    warnings: list[str] = []

    for spec in ARTIFACT_SPECS:
        raw_paths = _iter_state_paths(state.get(spec.state_key), many=spec.many)
        for item_index, raw_path in enumerate(raw_paths, start=1):
            source = Path(raw_path).expanduser()
            if not source.is_file():
                missing.append({'state_key': spec.state_key, 'role': spec.role, 'source_path': str(source)})
                warnings.append(f'missing artifact for {spec.role}: {source}')
                continue
            destination = _artifact_destination(packet_dir, spec, source, item_index=item_index)
            _copy_artifact(source=source, destination=destination)
            artifacts.append(_artifact_record(spec=spec, source=source, destination=destination))

    if not artifacts:
        raise ProofPacketError('No existing delivery artifacts are recorded in local CLI state; run save/export/page-screenshot first.')
    delivery_ready, delivery_ready_reason = _delivery_ready(artifacts)
    if not delivery_ready:
        warnings.append(f'packet is not delivery-ready: {delivery_ready_reason}')

    manifest: dict[str, Any] = {
        'schema_version': 'local-cli/proof-packet/v1',
        'ok': True,
        'created_at': _dt.datetime.now(_dt.UTC).isoformat(),
        'source_hwp_path': state.get('source_path'),
        'source_filename': state.get('source_filename'),
        'session_id': state.get('session_id'),
        'state_path': str(state_path.expanduser()) if state_path is not None else None,
        'packet_dir': str(packet_dir),
        'manifest_path': str(manifest_path),
        'artifacts': artifacts,
        'missing': missing,
        'warnings': warnings,
        'review_required': True,
        'delivery_ready': delivery_ready,
        'delivery_ready_reason': delivery_ready_reason,
        'next_step': 'Review the rendered page proof/PDF and manifest before delivery; this command only collects existing artifacts.',
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest
