from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.atomic_json import atomic_write_json
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
    ProofArtifactSpec('last_native_border_readback_path', 'native_border_readback', '.json'),
)


DELIVERY_SOURCE_ROLES = {'saved_working_copy', 'exported_pdf'}
RENDERED_PROOF_ROLES = {'rendered_page_proof', 'export_proof_page', 'export_proof_contact_sheet'}


def _sha256_file(path: Path) -> str:
    _assert_safe_artifact_file(path)
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_safe_artifact_file(path: Path) -> Path:
    """Reject symlink/reparse substitutions before reading proof bytes."""

    candidate = path.expanduser().absolute()
    current = candidate
    while True:
        if current.is_symlink():
            raise ProofPacketError(f'proof artifact path contains a symlink: {candidate}')
        parent = current.parent
        if parent == current:
            break
        current = parent
    if not candidate.is_file():
        raise ProofPacketError(f'proof artifact file is missing: {candidate}')
    return candidate


def _assert_safe_artifact_destination(path: Path) -> Path:
    """Reject destination symlink/reparse substitutions before writing."""

    candidate = path.expanduser().absolute()
    current = candidate
    while True:
        if current.is_symlink():
            raise ProofPacketError(f'proof artifact destination contains a symlink: {candidate}')
        if current.exists() and current != candidate and not current.is_dir():
            raise ProofPacketError(f'proof artifact destination parent is not a directory: {current}')
        parent = current.parent
        if parent == current:
            break
        current = parent
    if candidate.exists() and not candidate.is_file():
        raise ProofPacketError(f'proof artifact destination is not a regular file: {candidate}')
    return candidate


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
    source = _assert_safe_artifact_file(source)
    destination = _assert_safe_artifact_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source.resolve() == destination.resolve():
            return destination
    except FileNotFoundError:
        pass
    _assert_safe_artifact_destination(destination.parent / destination.name)
    temporary_path: Path | None = None
    try:
        with source.open('rb') as source_handle, tempfile.NamedTemporaryFile(
            mode='wb',
            prefix=f'.{destination.name}.',
            suffix='.tmp',
            dir=destination.parent,
            delete=False,
        ) as destination_handle:
            temporary_path = Path(destination_handle.name)
            shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    _assert_safe_artifact_file(destination)
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


def _artifact_record(
    *,
    spec: ProofArtifactSpec,
    source: Path,
    destination: Path,
    proof_binding: dict[str, Any] | None = None,
    required_sha256: str | None = None,
) -> dict[str, Any]:
    actual_sha256 = _sha256_file(destination)
    if required_sha256 is not None:
        required = str(required_sha256).removeprefix('sha256:').lower()
        if required != actual_sha256:
            raise ProofPacketError(
                f'{spec.role} destination bytes do not match the manifest-bound hash: '
                f'expected sha256:{required}, got sha256:{actual_sha256}'
            )
    record = {
        'role': spec.role,
        'state_key': spec.state_key,
        'source_path': str(source),
        'packet_path': str(destination),
        'relative_path': destination.name,
        'sha256': actual_sha256,
        'bytes': destination.stat().st_size,
    }
    if required_sha256 is not None:
        record['required_sha256'] = f"sha256:{str(required_sha256).removeprefix('sha256:').lower()}"
        record['hash_verified'] = True
    if proof_binding and spec.role in RENDERED_PROOF_ROLES | {'exported_pdf', 'export_manifest', 'export_proof_manifest', 'native_border_readback'}:
        record['proof_binding'] = dict(proof_binding)
    return record


def _read_mapping(path: Path) -> dict[str, Any] | None:
    try:
        safe_path = _assert_safe_artifact_file(path)
        payload = json.loads(safe_path.read_text(encoding='utf-8'))
    except (OSError, ProofPacketError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_authenticated_candidate_identity() -> dict[str, Any]:
    """Read the installed marker independently of cached CLI state."""

    try:
        from .main import _read_candidate_identity
        identity = _read_candidate_identity()
    except Exception:
        return {}
    if not isinstance(identity, dict) or not identity.get('candidate_identity_authenticated'):
        return {}
    return identity


def _normalize_bound_hash(raw_value: Any, *, label: str) -> str:
    value = str(raw_value or '').strip().lower()
    if value.startswith('sha256:'):
        value = value[len('sha256:'):]
    if len(value) != 64 or any(character not in '0123456789abcdef' for character in value):
        raise ProofPacketError(f'{label} is missing a valid manifest-bound SHA-256')
    return value


def _resolved_bound_path(raw_value: Any, *, manifest_path: Path) -> Path | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    try:
        return path.resolve()
    except OSError:
        return None


def _bound_export_hashes(
    *,
    state: dict[str, Any],
    proof_manifest: dict[str, Any] | None,
    proof_manifest_path: Path | None,
) -> tuple[str | None, dict[Path, str]]:
    """Return required PDF/page hashes for a generation-bound export proof."""

    expected_generation = state.get('last_export_proof_generation')
    if not expected_generation:
        return None, {}
    if proof_manifest is None or proof_manifest_path is None:
        raise ProofPacketError('generation-bound export proof manifest is missing')
    if str(proof_manifest.get('export_generation') or '') != str(expected_generation):
        raise ProofPacketError('export proof generation does not match CLI state')
    exported_pdf_hash = _normalize_bound_hash(
        proof_manifest.get('exported_pdf_sha256'),
        label='export proof PDF hash',
    )
    expected_state_hash = state.get('last_export_proof_export_sha256')
    if expected_state_hash and _normalize_bound_hash(expected_state_hash, label='cached export PDF hash') != exported_pdf_hash:
        raise ProofPacketError('export proof PDF hash does not match cached CLI state')
    manifest_pdf_path = _resolved_bound_path(proof_manifest.get('exported_pdf_path'), manifest_path=proof_manifest_path)
    state_pdf_path = _resolved_bound_path(state.get('last_export_path'), manifest_path=proof_manifest_path)
    if manifest_pdf_path is None or state_pdf_path is None or manifest_pdf_path != state_pdf_path:
        raise ProofPacketError('export proof PDF path does not match cached CLI state')
    expected_manifest_hash = state.get('last_export_proof_manifest_sha256')
    if expected_manifest_hash:
        actual_manifest_hash = f'sha256:{_sha256_file(proof_manifest_path)}'
        if _normalize_bound_hash(actual_manifest_hash, label='export proof manifest hash') != _normalize_bound_hash(expected_manifest_hash, label='cached export proof manifest hash'):
            raise ProofPacketError('export proof manifest bytes do not match cached CLI state')
    expected_target = state.get('last_export_proof_target_identity')
    if expected_target is not None and proof_manifest.get('target_identity') != expected_target:
        raise ProofPacketError('export proof target identity does not match cached CLI state')

    pages = proof_manifest.get('pages')
    if not isinstance(pages, list):
        raise ProofPacketError('generation-bound export proof manifest must contain a pages list')
    page_hashes: dict[Path, str] = {}
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            raise ProofPacketError(f'export proof page entry {index} is not an object')
        page_path = _resolved_bound_path(page.get('png_path'), manifest_path=proof_manifest_path)
        if page_path is None:
            raise ProofPacketError(f'export proof page entry {index} is missing png_path')
        if page_path in page_hashes:
            raise ProofPacketError(f'export proof page entry {index} duplicates png_path')
        page_hashes[page_path] = _normalize_bound_hash(
            page.get('png_sha256') or page.get('sha256'),
            label=f'export proof page {index} hash',
        )
    state_page_paths = _iter_state_paths(state.get('last_export_proof_page_paths'), many=True)
    resolved_state_pages = {
        path.resolve()
        for path in (Path(raw).expanduser() for raw in state_page_paths)
    }
    if resolved_state_pages != set(page_hashes):
        raise ProofPacketError('cached export proof page paths do not match the manifest-bound pages')
    return f'sha256:{exported_pdf_hash}', page_hashes


def _delivery_ready(artifacts: list[dict[str, Any]]) -> tuple[bool, str]:
    roles = {str(item.get('role') or '') for item in artifacts}
    has_source = bool(roles & DELIVERY_SOURCE_ROLES)
    has_rendered_proof = bool(roles & RENDERED_PROOF_ROLES)
    if has_source and has_rendered_proof:
        return True, 'packet includes a delivery source plus rendered proof artifact'
    if not has_rendered_proof:
        return False, 'packet is missing rendered proof artifact'
    return False, 'packet is missing saved/exported delivery source artifact'


def _state_proof_binding(state: dict[str, Any]) -> dict[str, Any]:
    candidate_identity = state.get('candidate_identity')
    candidate_identity = candidate_identity if isinstance(candidate_identity, dict) else {}
    values = {
        'candidate_generation': state.get('candidate_generation') or candidate_identity.get('candidate_generation'),
        'repository': state.get('repository') or candidate_identity.get('repository'),
        'commit': state.get('commit') or candidate_identity.get('commit'),
        'tree': state.get('tree') or candidate_identity.get('tree'),
        'source_manifest_sha256': (
            state.get('source_manifest_sha256')
            or state.get('manifest_sha256')
            or candidate_identity.get('source_manifest_sha256')
            or candidate_identity.get('manifest_sha256')
        ),
        'session_id': state.get('session_id'),
        'target_identity': (
            state.get('target_identity')
            or state.get('native_border_target_identity')
            or state.get('last_export_proof_target_identity')
        ),
    }
    return {key: value for key, value in values.items() if value not in (None, '', [])}


def _normalize_border_values(value: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Normalize a bounded value-level border map for exact comparison."""

    if depth >= 4 or len(value) > 32:
        raise ProofPacketError('native border readback contains an oversized or deeply nested value map')
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip() or len(raw_key) > 128:
            raise ProofPacketError('native border readback contains an invalid field name')
        if isinstance(raw_value, dict):
            normalized[raw_key] = _normalize_border_values(raw_value, depth=depth + 1)
        elif isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
            if isinstance(raw_value, str) and len(raw_value) > 256:
                raise ProofPacketError('native border readback contains an oversized field value')
            normalized[raw_key] = raw_value
        else:
            raise ProofPacketError('native border readback contains an unsupported field value')
    return {key: normalized[key] for key in sorted(normalized, key=str.casefold)}


def _border_values_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare normalized JSON values without bool/int coercion."""

    try:
        return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) == json.dumps(
            right,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def _validate_native_border_readback(
    path: Path,
    *,
    state: dict[str, Any],
    proof_binding: dict[str, Any],
) -> tuple[bool, str]:
    payload = _read_mapping(path)
    if payload is None:
        return False, f'native border readback is not a JSON object: {path}'
    if payload.get('schema_version') != 'local-cli/native-border-readback/v1':
        return False, f'native border readback schema is not value-level/v1: {path}'
    expected_generation = proof_binding.get('candidate_generation')
    expected_manifest = proof_binding.get('source_manifest_sha256')
    expected_target = proof_binding.get('target_identity')
    actual_generation = payload.get('candidate_generation')
    actual_manifest = payload.get('source_manifest_sha256') or payload.get('manifest_sha256')
    actual_target = payload.get('target_identity')
    if not expected_generation or not expected_manifest or expected_target is None:
        return False, 'native border readback is missing an independent candidate/source/target binding in CLI state'
    if actual_generation != expected_generation:
        return False, 'native border readback candidate generation does not match CLI state'
    if actual_manifest != expected_manifest:
        return False, 'native border readback source manifest hash does not match CLI state'
    if not isinstance(expected_target, dict) or not isinstance(actual_target, dict):
        return False, 'native border readback target identity must be a bounded object'
    try:
        normalized_expected_target = _normalize_border_values(expected_target)
        normalized_actual_target = _normalize_border_values(actual_target)
    except ProofPacketError as exc:
        return False, f'native border target identity is invalid: {exc}'
    if not _border_values_equal(normalized_actual_target, normalized_expected_target):
        return False, 'native border readback target identity does not match CLI state'
    if not isinstance(payload.get('pre_quit_readback'), dict) or not isinstance(payload.get('persisted_readback'), dict):
        return False, 'native border readback must include both pre_quit_readback and persisted_readback objects'
    if not payload['pre_quit_readback'] or not payload['persisted_readback']:
        return False, 'native border readback pre_quit_readback and persisted_readback must be non-empty'
    try:
        pre_quit = _normalize_border_values(payload['pre_quit_readback'])
        persisted = _normalize_border_values(payload['persisted_readback'])
    except ProofPacketError as exc:
        return False, str(exc)
    if not _border_values_equal(pre_quit, persisted):
        return False, 'native border pre_quit_readback and persisted_readback do not match'
    return True, ''


def seal_native_border_readback(
    *,
    destination: Path,
    candidate_generation: str,
    source_manifest_sha256: str,
    target_identity: dict[str, Any],
    pre_quit_readback: dict[str, Any],
    persisted_readback: dict[str, Any],
) -> dict[str, Any]:
    """Write the value-level native border persistence evidence packet.

    Native QA calls this after reading the selected cell before save/quit and
    again after reopening the saved document.  The candidate/source/target
    coordinates are part of the same atomically persisted JSON object so a
    later proof-packet collector can reject an otherwise plausible stale
    readback.
    """

    if not str(candidate_generation).strip():
        raise ProofPacketError('native border readback candidate_generation is required')
    if not str(source_manifest_sha256).strip():
        raise ProofPacketError('native border readback source_manifest_sha256 is required')
    if not isinstance(target_identity, dict) or not target_identity:
        raise ProofPacketError('native border readback target_identity is required')
    if not isinstance(pre_quit_readback, dict) or not pre_quit_readback:
        raise ProofPacketError('native border readback pre_quit_readback is required')
    if not isinstance(persisted_readback, dict) or not persisted_readback:
        raise ProofPacketError('native border readback persisted_readback is required')
    normalized_target = _normalize_border_values(target_identity)
    normalized_pre_quit = _normalize_border_values(pre_quit_readback)
    normalized_persisted = _normalize_border_values(persisted_readback)
    if not _border_values_equal(normalized_pre_quit, normalized_persisted):
        raise ProofPacketError('native border pre_quit_readback and persisted_readback do not match')
    payload: dict[str, Any] = {
        'schema_version': 'local-cli/native-border-readback/v1',
        'candidate_generation': str(candidate_generation),
        'source_manifest_sha256': str(source_manifest_sha256),
        'target_identity': normalized_target,
        'pre_quit_readback': normalized_pre_quit,
        'persisted_readback': normalized_persisted,
    }
    atomic_write_json(Path(destination).expanduser(), payload)
    return payload


def build_proof_packet(*, out_dir: Path, state: dict[str, Any], state_path: Path | None = None) -> dict[str, Any]:
    packet_dir = out_dir.expanduser()
    packet_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = packet_dir / 'manifest.json'
    artifacts: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    warnings: list[str] = []
    state_binding = _state_proof_binding(state)
    proof_binding = {
        **state_binding,
        'generation': state.get('last_export_proof_generation'),
        'exported_pdf_sha256': state.get('last_export_proof_export_sha256'),
        'manifest_sha256': state.get('last_export_proof_manifest_sha256'),
        'session_id': state.get('last_export_proof_session_id') or state_binding.get('session_id'),
        'target_identity': state.get('last_export_proof_target_identity') or state_binding.get('target_identity'),
    }
    proof_binding = {key: value for key, value in proof_binding.items() if value not in (None, '', [])}
    proof_generation = state.get('last_export_proof_proof_generation') or state.get('proof_generation')
    candidate_identity: dict[str, Any] = (
        state.get('candidate_identity') if isinstance(state.get('candidate_identity'), dict) else {}
    )
    identity_verified = bool(
        state.get('candidate_identity_authenticated')
        or (
            candidate_identity.get('identity_source') == 'git'
            and candidate_identity.get('identity_verified') is True
        )
        or (
            state.get('identity_source') == 'git'
            and state.get('identity_verified') is True
        )
    )
    installed_identity = _read_authenticated_candidate_identity()
    if not installed_identity or any(
        installed_identity.get(field) != proof_binding.get(field)
        for field in ('candidate_generation', 'repository', 'commit', 'tree', 'source_manifest_sha256')
    ):
        identity_verified = False
    proof_manifest_path = state.get('last_export_proof_manifest_path')
    proof_manifest = _read_mapping(Path(str(proof_manifest_path)).expanduser()) if proof_manifest_path else None
    resolved_proof_manifest_path = Path(str(proof_manifest_path)).expanduser().resolve() if proof_manifest_path else None
    bound_pdf_hash, bound_page_hashes = _bound_export_hashes(
        state=state,
        proof_manifest=proof_manifest,
        proof_manifest_path=resolved_proof_manifest_path,
    )
    stale_export_proof = False
    expected_generation = state.get('last_export_proof_generation')
    if expected_generation:
        if proof_manifest is None or str(proof_manifest.get('export_generation') or '') != str(expected_generation):
            stale_export_proof = True
            warnings.append('export proof generation could not be matched to the cached proof manifest; export proof was not collected')
        expected_export_hash = str(state.get('last_export_proof_export_sha256') or '')
        manifest_export_hash = str(proof_manifest.get('exported_pdf_sha256') or '') if proof_manifest else ''
        if expected_export_hash and manifest_export_hash and expected_export_hash != manifest_export_hash:
            stale_export_proof = True
            warnings.append('export proof PDF hash did not match the cached proof manifest; export proof was not collected')
        expected_manifest_hash = str(state.get('last_export_proof_manifest_sha256') or '')
        if expected_manifest_hash and proof_manifest_path:
            manifest_file = Path(str(proof_manifest_path)).expanduser()
            if not manifest_file.is_file() or f'sha256:{_sha256_file(manifest_file)}' != expected_manifest_hash:
                stale_export_proof = True
                warnings.append('export proof manifest hash did not match cached state; export proof was not collected')
        expected_target = state.get('last_export_proof_target_identity')
        if expected_target is not None and proof_manifest is not None and proof_manifest.get('target_identity') is not None:
            if expected_target != proof_manifest.get('target_identity'):
                stale_export_proof = True
                warnings.append('export proof target identity did not match cached state; export proof was not collected')

    authenticated_proof = bool(
        identity_verified
        and proof_generation
        and bound_pdf_hash
        and bound_page_hashes
        and proof_binding.get('candidate_generation')
        and proof_binding.get('source_manifest_sha256')
    )
    manifest_identity = proof_manifest.get('candidate_identity') if isinstance(proof_manifest, dict) else None
    if authenticated_proof:
        authenticated_proof = bool(
            isinstance(manifest_identity, dict)
            and manifest_identity.get('identity_source') == 'git'
            and manifest_identity.get('identity_verified') is True
            and manifest_identity.get('candidate_generation') == proof_binding.get('candidate_generation')
            and manifest_identity.get('source_manifest_sha256') == proof_binding.get('source_manifest_sha256')
            and proof_manifest.get('proof_generation') == proof_generation
        )

    for spec in ARTIFACT_SPECS:
        if stale_export_proof and spec.role in RENDERED_PROOF_ROLES | {'exported_pdf', 'export_manifest', 'export_proof_manifest'}:
            warnings.append(f'skipped stale export artifact role: {spec.role}')
            continue
        raw_paths = _iter_state_paths(state.get(spec.state_key), many=spec.many)
        for item_index, raw_path in enumerate(raw_paths, start=1):
            source = Path(raw_path).expanduser()
            required_hash = None
            if spec.role == 'saved_working_copy':
                required_hash = state.get('last_saved_working_copy_sha256')
            elif spec.role == 'exported_pdf':
                required_hash = bound_pdf_hash
            elif spec.role == 'export_proof_contact_sheet':
                required_hash = state.get('last_export_proof_contact_sheet_sha256')
            elif spec.role == 'export_proof_manifest':
                required_hash = state.get('last_export_proof_manifest_sha256')
            elif spec.role == 'export_proof_page' and bound_page_hashes:
                try:
                    required_hash = bound_page_hashes.get(source.resolve())
                except OSError:
                    required_hash = None
                if required_hash is None:
                    raise ProofPacketError(f'export proof page is not present in the manifest-bound page set: {source}')
            if not source.is_file():
                if required_hash is not None:
                    raise ProofPacketError(f'{spec.role} source file is missing: {source}')
                missing.append({'state_key': spec.state_key, 'role': spec.role, 'source_path': str(source)})
                warnings.append(f'missing artifact for {spec.role}: {source}')
                continue
            if spec.role == 'native_border_readback':
                valid, reason = _validate_native_border_readback(source, state=state, proof_binding=proof_binding)
                if not valid:
                    warnings.append(f'skipped unbound native border readback: {reason}')
                    continue
            if required_hash is not None:
                actual_source_hash = _sha256_file(source)
                if actual_source_hash != str(required_hash).removeprefix('sha256:').lower():
                    raise ProofPacketError(
                        f'{spec.role} source bytes do not match the manifest-bound hash: {source}'
                    )
            destination = _artifact_destination(packet_dir, spec, source, item_index=item_index)
            _copy_artifact(source=source, destination=destination)
            artifacts.append(
                _artifact_record(
                    spec=spec,
                    source=source,
                    destination=destination,
                    proof_binding=proof_binding,
                    required_sha256=required_hash,
                )
            )

    if not artifacts:
        raise ProofPacketError('No existing delivery artifacts are recorded in local CLI state; run save/export/page-screenshot first.')
    bound_artifact_roles = DELIVERY_SOURCE_ROLES | RENDERED_PROOF_ROLES | {'export_proof_manifest'}
    all_delivery_bytes_bound = all(
        item.get('hash_verified') is True
        for item in artifacts
        if item.get('role') in bound_artifact_roles
    )
    if not all_delivery_bytes_bound:
        authenticated_proof = False
    delivery_ready, delivery_ready_reason = _delivery_ready(artifacts)
    if not authenticated_proof and delivery_ready:
        delivery_ready = False
        delivery_ready_reason = 'packet is missing authenticated candidate-bound proof generation and artifact hashes'
        warnings.append('packet is not delivery-ready: authenticated candidate-bound proof generation is required')
    if not delivery_ready:
        warnings.append(f'packet is not delivery-ready: {delivery_ready_reason}')

    manifest: dict[str, Any] = {
        'schema_version': 'local-cli/proof-packet/v1',
        'ok': True,
        'created_at': _dt.datetime.now(_dt.UTC).isoformat(),
        'source_hwp_path': state.get('source_path'),
        'source_filename': state.get('source_filename'),
        'session_id': state.get('session_id'),
        'proof_binding': proof_binding,
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
    atomic_write_json(manifest_path, manifest)
    return manifest
