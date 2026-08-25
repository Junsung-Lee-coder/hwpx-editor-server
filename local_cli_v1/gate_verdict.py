from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .output_parser import summarize_readback


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text_from_block(block: Mapping[str, Any]) -> str:
    text = _as_dict(block.get('text'))
    return str(text.get('preview') or text.get('value') or block.get('text_preview') or '')


def _collect_text(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ('outside_text_blocks', 'table_cells'):
        for block in _as_list(payload.get(key)):
            if isinstance(block, Mapping):
                parts.append(_text_from_block(block))
    static_text = _as_dict(payload.get('text')).get('preview')
    if static_text:
        parts.append(str(static_text))
    return '\n'.join(part for part in parts if part)


def _count(payload: Mapping[str, Any], key: str) -> Any:
    structure = _as_dict(payload.get('structure_summary'))
    if key in structure:
        return structure.get(key)
    document = _as_dict(payload.get('document'))
    if key == 'page_count':
        return document.get('page_count')
    return None


def _load_render_manifest(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    manifest_path = Path(path).expanduser()
    if not manifest_path.exists() or not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _raw_readback_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = _as_dict(payload)
    if raw.get('schema_version') == 'local-output-parser/readback/v1':
        return raw
    for raw_step in _as_list(raw.get('steps')):
        if not isinstance(raw_step, Mapping):
            continue
        step = _as_dict(raw_step)
        if step.get('op') == 'readback':
            result = _as_dict(step.get('result'))
            if result:
                return result
    result = _as_dict(raw.get('result'))
    if result:
        return result
    return raw


def _has_explicit_value(payload: Mapping[str, Any], key: str) -> bool:
    return key in payload and payload.get(key) not in (None, '')


def _raw_count(payload: Mapping[str, Any], key: str) -> Any:
    structure = _as_dict(payload.get('structure_summary'))
    if key in structure:
        return structure.get(key)
    document = _as_dict(payload.get('document'))
    if key == 'page_count':
        return document.get('page_count')
    return None


def _has_count_evidence(payload: Mapping[str, Any], key: str) -> bool:
    structure = _as_dict(payload.get('structure_summary'))
    if _has_explicit_value(structure, key):
        return True
    if key == 'page_count':
        return _has_explicit_value(_as_dict(payload.get('document')), 'page_count')
    return False


def _preserve_gate_evidence(summary: dict[str, Any], manifest: Mapping[str, Any]) -> None:
    raw = _raw_readback_payload(manifest)
    for key in ('input_sha256', 'authority'):
        if key in manifest:
            summary[key] = manifest[key]
        elif key in raw:
            summary[key] = raw[key]
    if 'images' in raw:
        summary['images'] = _as_list(raw.get('images'))


def _require_primary_evidence(issues: list[dict[str, Any]], *, side: str, manifest: Mapping[str, Any]) -> None:
    raw = _raw_readback_payload(manifest)
    missing_count_keys = [
        key
        for key in ('page_count', 'table_count', 'control_count', 'image_like_control_count')
        if not _has_count_evidence(raw, key)
    ]
    if missing_count_keys:
        _add_issue(
            issues,
            severity='FAIL',
            risk_flag='count_evidence_missing',
            message=f'{side} manifest lacks explicit structure/page count evidence',
            side=side,
            missing=missing_count_keys,
        )

    control_count = _raw_count(raw, 'control_count')
    controls = raw.get('controls')
    if control_count not in (None, '', 0) and (not isinstance(controls, list) or not controls):
        _add_issue(
            issues,
            severity='FAIL',
            risk_flag='control_inventory_missing',
            message=f'{side} manifest reports controls but lacks explicit control inventory evidence',
            side=side,
            expected_count=control_count,
        )

    image_count = _raw_count(raw, 'image_like_control_count')
    images = raw.get('images')
    if image_count not in (None, '', 0) and (not isinstance(images, list) or not images):
        _add_issue(
            issues,
            severity='FAIL',
            risk_flag='image_inventory_missing',
            message=f'{side} manifest reports image-like controls but lacks explicit image inventory evidence',
            side=side,
            expected_count=image_count,
        )


def _path_from_manifest(value: Any, *, base_dir: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _render_artifact_paths(payload: Mapping[str, Any], *, base_dir: Path) -> dict[str, list[Path]]:
    pdf_paths: list[Path] = []
    visual_paths: list[Path] = []
    for key in ('exported_pdf_path', 'pdf_path', 'rendered_pdf_path'):
        path = _path_from_manifest(payload.get(key), base_dir=base_dir)
        if path is not None:
            pdf_paths.append(path)
    for key in ('contact_sheet_path', 'contact_sheet'):
        path = _path_from_manifest(payload.get(key), base_dir=base_dir)
        if path is not None:
            visual_paths.append(path)
    for raw_path in _as_list(payload.get('page_pngs')):
        path = _path_from_manifest(raw_path, base_dir=base_dir)
        if path is not None:
            visual_paths.append(path)
    for collection_key in ('pages', 'page_images', 'rendered_pages', 'artifacts', 'items'):
        for item in _as_list(payload.get(collection_key)):
            if not isinstance(item, Mapping):
                continue
            for key in ('png_path', 'path', 'image_path', 'rendered_path', 'output_path', 'contact_sheet_path'):
                path = _path_from_manifest(_as_dict(item).get(key), base_dir=base_dir)
                if path is not None:
                    visual_paths.append(path)
    return {'pdf': pdf_paths, 'visual': visual_paths}


def _render_manifest_validation(payload: Mapping[str, Any] | None, manifest_path: str | Path | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {'trusted': False, 'valid': False, 'pdf_paths': [], 'visual_paths': [], 'missing_paths': []}
    base_dir = Path(manifest_path).expanduser().parent if manifest_path is not None else Path.cwd()
    paths = _render_artifact_paths(payload, base_dir=base_dir)
    all_paths = [*paths['pdf'], *paths['visual']]
    missing_paths = [path for path in all_paths if not path.exists() or not path.is_file()]
    trusted = payload.get('schema_version') == 'local-cli/export-proof-range/v1' and payload.get('ok') is True
    valid = bool(trusted and paths['pdf'] and paths['visual'] and not missing_paths)
    return {
        'trusted': trusted,
        'valid': valid,
        'schema_version': payload.get('schema_version'),
        'ok': payload.get('ok'),
        'pdf_paths': [str(path) for path in paths['pdf']],
        'visual_paths': [str(path) for path in paths['visual']],
        'missing_paths': [str(path) for path in missing_paths],
    }


def _fingerprints(payload: Mapping[str, Any], collection_key: str) -> list[str]:
    values: list[str] = []
    for raw_item in _as_list(payload.get(collection_key)):
        if not isinstance(raw_item, Mapping):
            continue
        item = _as_dict(raw_item)
        digest = item.get('proof_hash') or item.get('sha256') or item.get('text_hash')
        if not digest:
            continue
        identity = item.get('target_id') or item.get('id') or item.get('source_path') or item.get('type') or collection_key
        values.append(f'{identity}:{digest}')
    return sorted(values)


def _add_fingerprint_drift(
    issues: list[dict[str, Any]],
    *,
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    collection_key: str,
    risk_flag: str,
    allowed: set[str],
) -> None:
    if collection_key in allowed or risk_flag in allowed:
        return
    source_values = _fingerprints(source, collection_key)
    candidate_values = _fingerprints(candidate, collection_key)
    if source_values and candidate_values and source_values != candidate_values:
        _add_issue(
            issues,
            severity='FAIL',
            risk_flag=risk_flag,
            message=f'{collection_key} proof hashes differ without allowlist',
            source=source_values,
            candidate=candidate_values,
        )


def _add_issue(issues: list[dict[str, Any]], *, severity: str, risk_flag: str, message: str, **extra: Any) -> None:
    payload = {'severity': severity, 'risk_flag': risk_flag, 'message': message}
    payload.update(extra)
    issues.append(payload)


def _verdict(issues: list[dict[str, Any]]) -> str:
    return 'FAIL' if any(str(issue.get('severity')) == 'FAIL' for issue in issues) else 'PASS'


def summarize_gate_verdict(
    source_manifest: Mapping[str, Any],
    candidate_manifest: Mapping[str, Any],
    *,
    planned_mutations: int = 0,
    require_tokens: list[str] | None = None,
    forbid_tokens: list[str] | None = None,
    render_manifest: str | Path | None = None,
    static_supplement: Mapping[str, Any] | None = None,
    allow_count_drift: list[str] | None = None,
) -> dict[str, Any]:
    """Summarize production gate evidence without mutating documents.

    The gate treats Hancom-native readback/render as primary. Static supplements
    are preserved as secondary diagnostics and can never upgrade a failing gate.
    """

    source = summarize_readback(source_manifest)
    candidate = summarize_readback(candidate_manifest)
    # Preserve gate evidence if summarize_readback did not include it.
    _preserve_gate_evidence(source, source_manifest)
    _preserve_gate_evidence(candidate, candidate_manifest)

    issues: list[dict[str, Any]] = []
    source_hash = source.get('input_sha256')
    candidate_hash = candidate.get('input_sha256')
    if not source_hash:
        _add_issue(issues, severity='FAIL', risk_flag='source_hash_missing', message='source manifest lacks required input_sha256')
    if not candidate_hash:
        _add_issue(issues, severity='FAIL', risk_flag='candidate_hash_missing', message='candidate manifest lacks required input_sha256')
    _require_primary_evidence(issues, side='source', manifest=source_manifest)
    _require_primary_evidence(issues, side='candidate', manifest=candidate_manifest)
    if planned_mutations > 0 and source_hash and candidate_hash and source_hash == candidate_hash:
        _add_issue(
            issues,
            severity='FAIL',
            risk_flag='source_copy_trap',
            message='planned mutation count is non-zero but source/candidate hashes are identical',
            source_hash=source_hash,
            candidate_hash=candidate_hash,
        )

    candidate_text = _collect_text(candidate)
    for token in require_tokens or []:
        if token and token not in candidate_text:
            _add_issue(issues, severity='FAIL', risk_flag='required_token_absent', message=f'required token absent: {token}', token=token)
    for token in forbid_tokens or []:
        if token and token in candidate_text:
            _add_issue(issues, severity='FAIL', risk_flag='forbidden_token_present', message=f'forbidden token present: {token}', token=token)

    allowed = set(allow_count_drift or [])
    count_keys = ('page_count', 'table_count', 'control_count', 'image_like_control_count')
    for key in count_keys:
        if key in allowed:
            continue
        sv = _count(source, key)
        cv = _count(candidate, key)
        if sv not in (None, '') and cv not in (None, '') and sv != cv:
            _add_issue(issues, severity='FAIL', risk_flag=f'{key}_drift', message=f'{key} differs without allowlist', source=sv, candidate=cv)
    _add_fingerprint_drift(issues, source=source, candidate=candidate, collection_key='controls', risk_flag='control_hash_drift', allowed=allowed)
    _add_fingerprint_drift(issues, source=source, candidate=candidate, collection_key='images', risk_flag='image_hash_drift', allowed=allowed)

    render_payload = _load_render_manifest(render_manifest)
    render_validation = _render_manifest_validation(render_payload, render_manifest)
    render_visual_proof = bool(render_validation.get('valid'))
    if render_payload is None:
        _add_issue(issues, severity='FAIL', risk_flag='render_proof_missing', message='Hancom-native render/contact-sheet manifest is missing or unreadable')
    else:
        if not render_validation.get('trusted'):
            _add_issue(
                issues,
                severity='FAIL',
                risk_flag='render_proof_untrusted',
                message='render manifest is not a successful local-cli/export-proof-range Hancom proof manifest',
                schema_version=render_validation.get('schema_version'),
                ok=render_validation.get('ok'),
            )
        if not render_validation.get('pdf_paths'):
            _add_issue(
                issues,
                severity='FAIL',
                risk_flag='render_artifact_missing',
                message='render manifest lacks required exported PDF artifact path',
                missing=['exported_pdf_path'],
            )
        if not render_validation.get('visual_paths'):
            _add_issue(
                issues,
                severity='FAIL',
                risk_flag='render_proof_non_visual',
                message='render manifest lacks rendered page/contact-sheet artifact paths; page count alone is not proof',
            )
        if render_validation.get('missing_paths'):
            _add_issue(
                issues,
                severity='FAIL',
                risk_flag='render_artifact_missing',
                message='render manifest references artifact paths that do not exist on disk',
                missing=render_validation.get('missing_paths'),
            )
    if render_payload is not None and not render_visual_proof and not any(str(issue.get('risk_flag')) == 'render_proof_non_visual' for issue in issues):
        _add_issue(issues, severity='FAIL', risk_flag='render_proof_non_visual', message='render manifest lacks rendered page/contact-sheet/PDF artifact paths; page count alone is not proof')

    static_payload = dict(static_supplement or {})
    if static_payload:
        _add_issue(
            issues,
            severity='WARN',
            risk_flag='static_supplement_secondary_only',
            message='static supplement retained as secondary diagnostic evidence only',
            authority=static_payload.get('authority'),
            qa_claim_allowed=static_payload.get('qa_claim_allowed'),
        )
        if static_payload.get('qa_claim_allowed') is not False:
            _add_issue(issues, severity='FAIL', risk_flag='static_supplement_authority_label_missing', message='static supplement lacks fail-closed qa_claim_allowed=false label')

    verdict = _verdict(issues)
    return {
        'schema_version': 'local-cli/gate-verdict/v1',
        'ok': True,
        'read_only': True,
        'authority': 'hancom_native_primary_gate',
        'verdict': verdict,
        'external_send_allowed': False,
        'planned_mutations': planned_mutations,
        'source_hash': source_hash,
        'candidate_hash': candidate_hash,
        'render_manifest': str(Path(render_manifest).expanduser()) if render_manifest else None,
        'render_manifest_loaded': render_payload is not None,
        'render_proof_present': render_visual_proof,
        'render_validation': render_validation,
        'issues': issues,
        'risk_flags': sorted({str(issue.get('risk_flag')) for issue in issues if issue.get('risk_flag')}),
        'static_supplement': {
            'present': bool(static_payload),
            'authority': static_payload.get('authority'),
            'qa_claim_allowed': static_payload.get('qa_claim_allowed'),
            'summary': static_payload.get('structure_summary'),
        }
        if static_payload
        else {'present': False},
        'required_followup': 'external send remains held unless a separate user authorization/readback gate approves it',
    }


def load_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
