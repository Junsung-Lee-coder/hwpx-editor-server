from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .output_parser import summarize_readback


_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))
_SEVERITY_ORDER = {'PASS': 0, 'WARN': 1, 'FAIL': 2}
_TABLE_KINDS = {'tbl', 'table'}
_IMAGE_KINDS = {'pic', 'image', 'gso', 'shape', 'drawing', 'ole'}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    if value is None:
        return ''
    return str(value)


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    return _as_str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return _safe_scalar(value)


def _hash_bytes(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _hash_json(value: Any) -> str:
    data = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return _hash_bytes(data)


def _normalize(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = _as_dict(payload)
    normalized = summarize_readback(payload)
    for key in ('input_path', 'input_sha256'):
        if raw.get(key) not in (None, ''):
            normalized[key] = raw.get(key)
    return _as_dict(normalized)


def load_readback_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    with manifest_path.open('r', encoding='utf-8') as fh:
        payload = json.load(fh)
    normalized = _normalize(payload)
    normalized.setdefault('input_path', str(manifest_path))
    normalized.setdefault('input_sha256', _hash_bytes(manifest_path.read_bytes()))
    return normalized


def _counts_keys(counts: Any) -> set[str]:
    return {str(key) for key, value in _as_dict(counts).items() if key not in (None, '') and value not in (None, '', 0)}


def _float_value(value: Any) -> float | None:
    if value in (None, '') or isinstance(value, bool):
        return None
    try:
        return round(float(value), 3)
    except Exception:
        return None


def _size_keys(counts: Any) -> set[float | str]:
    result: set[float | str] = set()
    for key, value in _as_dict(counts).items():
        if value in (None, '', 0):
            continue
        parsed = _float_value(key)
        result.add(parsed if parsed is not None else str(key))
    return result


def _style_signature(style: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'font_family': style.get('font_family') or style.get('face_name'),
        'font_size_pt': _float_value(style.get('font_size_pt')),
        'bold': style.get('bold'),
        'align': style.get('align'),
        'line_spacing': style.get('line_spacing'),
    }


def _text_hash(block: Mapping[str, Any]) -> str | None:
    text = _as_dict(block.get('text'))
    value = text.get('normalized_hash') or text.get('hash')
    return str(value) if value not in (None, '') else None


def _table_meta(block: Mapping[str, Any]) -> dict[str, Any]:
    table = _as_dict(block.get('table'))
    return {
        'target_id': table.get('target_id') or table.get('id') or table.get('proof_hash') or block.get('block_id'),
        'cell_addr': table.get('cell_addr'),
        'row_1based': table.get('row_1based') or table.get('row'),
        'col_1based': table.get('col_1based') or table.get('col'),
        'row_count': table.get('row_count'),
        'col_count': table.get('col_count'),
        'row_span': table.get('row_span') or table.get('rowSpan'),
        'col_span': table.get('col_span') or table.get('colSpan'),
        'proof_hash': table.get('proof_hash'),
    }


def _block_key(block: Mapping[str, Any], *, inside_table: bool) -> str:
    if inside_table:
        table = _table_meta(block)
        target = table.get('target_id') or 'table'
        cell = table.get('cell_addr') or block.get('block_id') or _text_hash(block) or 'cell'
        return f'{target}:{cell}'
    return str(block.get('block_id') or _text_hash(block) or f'outside:{id(block)}')


def _block_inventory(blocks: list[Any], *, inside_table: bool) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for raw in blocks:
        block = _as_dict(raw)
        key = _block_key(block, inside_table=inside_table)
        inventory[key] = {
            'block_id': block.get('block_id'),
            'inside_table': inside_table,
            'text_hash': _text_hash(block),
            'text_preview': _as_dict(block.get('text')).get('preview'),
            'style': _style_signature(_as_dict(block.get('style'))),
            'table': _table_meta(block) if inside_table else None,
        }
    return inventory


def _control_kind(control: Mapping[str, Any]) -> str:
    return str(control.get('type') or control.get('ctrl_id') or '').strip().lower()


def _control_key(control: Mapping[str, Any]) -> str:
    return str(control.get('target_id') or control.get('ctrl_id') or control.get('proof_hash') or control.get('index') or _hash_json(control))


def _control_inventory(controls: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in controls:
        control = _as_dict(raw)
        result[_control_key(control)] = {
            'target_id': control.get('target_id'),
            'type': _control_kind(control),
            'page': control.get('page') or control.get('page_candidate'),
            'proof_hash': control.get('proof_hash'),
            'anchor': control.get('anchor') or control.get('anchor_hash') or control.get('position'),
        }
    return result


def _join_values(values: Any) -> str:
    if isinstance(values, set):
        material = sorted(str(value) for value in values)
    elif isinstance(values, list):
        material = [str(value) for value in values]
    else:
        material = [str(values)]
    return ', '.join(material) if material else 'none'


def _normalize_allowed_cells(values: Iterable[str] | None) -> set[str]:
    return {str(value or '').strip().upper() for value in (values or []) if str(value or '').strip()}


def _table_cell_addr(item: Mapping[str, Any]) -> str:
    table = _as_dict(item.get('table'))
    return str(table.get('cell_addr') or '').strip().upper()


def _has_current_block_only_text_arrays(payload: Mapping[str, Any]) -> bool:
    structure = _as_dict(payload.get('structure_summary'))
    return bool(
        structure.get('broad_text_block_enumeration_available') is False
        or structure.get('outside_text_blocks_scope') == 'current_block_only'
        or structure.get('table_cells_scope') == 'current_block_only'
    )


def _current_block_only_diff_note(source: Mapping[str, Any], candidate: Mapping[str, Any]) -> str | None:
    limited = []
    if _has_current_block_only_text_arrays(source):
        limited.append('source')
    if _has_current_block_only_text_arrays(candidate):
        limited.append('candidate')
    if not limited:
        return None
    return (
        f"{'/'.join(limited)} readback text/table arrays are current-block-only; "
        'skipping source-vs-candidate font/table-cell style identity diffs that would compare unrelated cursor positions. '
        'Use explicit find/style-inspect evidence for changed tokens.'
    )


def _add_issue(
    issues: list[dict[str, Any]],
    *,
    severity: str,
    category: str,
    risk_flag: str,
    message: str,
    source: Any = None,
    candidate: Any = None,
) -> None:
    issues.append(
        {
            'severity': severity,
            'category': category,
            'risk_flag': risk_flag,
            'message': message,
            'source': _jsonable(source),
            'candidate': _jsonable(candidate),
        }
    )


def _severity_from_issues(issues: list[dict[str, Any]]) -> str:
    severity = 'PASS'
    for issue in issues:
        current = str(issue.get('severity') or 'WARN')
        if _SEVERITY_ORDER.get(current, 1) > _SEVERITY_ORDER[severity]:
            severity = current
    return severity


def _diff_style_summary(source: Mapping[str, Any], candidate: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    source_style = _as_dict(source.get('style_summary'))
    candidate_style = _as_dict(candidate.get('style_summary'))

    source_families = _counts_keys(source_style.get('font_family_counts'))
    candidate_families = _counts_keys(candidate_style.get('font_family_counts'))
    source_body_family = source_style.get('body_font_family_candidate')
    candidate_body_family = candidate_style.get('body_font_family_candidate')
    if source_families != candidate_families or source_body_family != candidate_body_family:
        _add_issue(
            issues,
            severity='FAIL',
            category='font_family',
            risk_flag='font_family_drift',
            message=f'font family drift: source={_join_values(source_families or {source_body_family})} candidate={_join_values(candidate_families or {candidate_body_family})}',
            source={'families': sorted(source_families), 'body': source_body_family},
            candidate={'families': sorted(candidate_families), 'body': candidate_body_family},
        )

    source_sizes = _size_keys(source_style.get('font_size_pt_counts'))
    candidate_sizes = _size_keys(candidate_style.get('font_size_pt_counts'))
    source_body_size = _float_value(source_style.get('body_font_size_pt_candidate'))
    candidate_body_size = _float_value(candidate_style.get('body_font_size_pt_candidate'))
    if source_sizes != candidate_sizes or source_body_size != candidate_body_size:
        _add_issue(
            issues,
            severity='FAIL',
            category='font_size',
            risk_flag='font_size_drift',
            message=f'font size/body-size drift: source={_join_values(source_sizes or {source_body_size})} candidate={_join_values(candidate_sizes or {candidate_body_size})}',
            source={'sizes': sorted(str(value) for value in source_sizes), 'body': source_body_size},
            candidate={'sizes': sorted(str(value) for value in candidate_sizes), 'body': candidate_body_size},
        )
    if candidate_style.get('mixed_size_warning') and not source_style.get('mixed_size_warning'):
        _add_issue(
            issues,
            severity='FAIL',
            category='font_size',
            risk_flag='body_size_inconsistent',
            message='candidate reports mixed/body-size inconsistency not present in source',
            source={'mixed_size_warning': source_style.get('mixed_size_warning')},
            candidate={'mixed_size_warning': candidate_style.get('mixed_size_warning')},
        )
    if candidate_style.get('mixed_font_warning') and not source_style.get('mixed_font_warning'):
        _add_issue(
            issues,
            severity='FAIL',
            category='font_family',
            risk_flag='body_font_inconsistent',
            message='candidate reports mixed font-family inconsistency not present in source',
            source={'mixed_font_warning': source_style.get('mixed_font_warning')},
            candidate={'mixed_font_warning': candidate_style.get('mixed_font_warning')},
        )


def _diff_structure_counts(source: Mapping[str, Any], candidate: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    source_structure = _as_dict(source.get('structure_summary'))
    candidate_structure = _as_dict(candidate.get('structure_summary'))
    for key, risk_flag, category, severity in (
        ('table_count', 'native_table_drift', 'native_table', 'FAIL'),
        ('table_cell_count', 'native_table_drift', 'native_table', 'FAIL'),
        ('control_count', 'control_anchor_drift', 'controls', 'WARN'),
        ('image_like_control_count', 'control_anchor_drift', 'controls', 'WARN'),
    ):
        if source_structure.get(key) != candidate_structure.get(key):
            _add_issue(
                issues,
                severity=severity,
                category=category,
                risk_flag=risk_flag,
                message=f'{key} drift: source={source_structure.get(key)} candidate={candidate_structure.get(key)}',
                source={key: source_structure.get(key)},
                candidate={key: candidate_structure.get(key)},
            )


def _diff_table_cells(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    issues: list[dict[str, Any]],
    *,
    allowed_table_cell_content_changes: set[str] | None = None,
    allowed_changes: list[dict[str, Any]] | None = None,
) -> None:
    allowed_cells = allowed_table_cell_content_changes or set()
    source_cells = _block_inventory(_as_list(source.get('table_cells')), inside_table=True)
    candidate_cells = _block_inventory(_as_list(candidate.get('table_cells')), inside_table=True)
    source_keys = set(source_cells)
    candidate_keys = set(candidate_cells)
    if source_keys != candidate_keys:
        _add_issue(
            issues,
            severity='FAIL',
            category='native_table',
            risk_flag='native_table_drift',
            message=f'native table cell identity drift: missing={_join_values(source_keys - candidate_keys)} added={_join_values(candidate_keys - source_keys)}',
            source=sorted(source_keys),
            candidate=sorted(candidate_keys),
        )
    for key in sorted(source_keys & candidate_keys):
        src = source_cells[key]
        cand = candidate_cells[key]
        src_table = _as_dict(src.get('table'))
        cand_table = _as_dict(cand.get('table'))
        structure_keys = ('row_count', 'col_count', 'row_span', 'col_span', 'row_1based', 'col_1based', 'cell_addr')
        source_structure = {name: src_table.get(name) for name in structure_keys}
        candidate_structure = {name: cand_table.get(name) for name in structure_keys}
        if source_structure != candidate_structure:
            _add_issue(
                issues,
                severity='FAIL',
                category='native_table',
                risk_flag='native_table_drift',
                message=f'native table structure drift at {key}',
                source=source_structure,
                candidate=candidate_structure,
            )
        if src.get('text_hash') != cand.get('text_hash'):
            cell_addr = _table_cell_addr(cand) or _table_cell_addr(src)
            if cell_addr in allowed_cells:
                if allowed_changes is not None:
                    allowed_changes.append(
                        {
                            'category': 'native_table',
                            'risk_flag': 'planned_table_cell_content_change',
                            'cell_addr': cell_addr,
                            'key': key,
                            'source': {'text_hash': src.get('text_hash'), 'preview': src.get('text_preview')},
                            'candidate': {'text_hash': cand.get('text_hash'), 'preview': cand.get('text_preview')},
                        }
                    )
                continue
            _add_issue(
                issues,
                severity='FAIL',
                category='native_table',
                risk_flag='native_table_drift',
                message=f'native table cell content drift at {key}',
                source={'text_hash': src.get('text_hash'), 'preview': src.get('text_preview')},
                candidate={'text_hash': cand.get('text_hash'), 'preview': cand.get('text_preview')},
            )
        if _as_dict(src.get('style')) != _as_dict(cand.get('style')):
            _add_issue(
                issues,
                severity='FAIL',
                category='native_table',
                risk_flag='inside_table_style_drift',
                message=f'inside-table style drift at {key}',
                source=src.get('style'),
                candidate=cand.get('style'),
            )


def _diff_inside_outside(source: Mapping[str, Any], candidate: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    source_outside = _block_inventory(_as_list(source.get('outside_text_blocks')), inside_table=False)
    candidate_outside = _block_inventory(_as_list(candidate.get('outside_text_blocks')), inside_table=False)
    source_table = _block_inventory(_as_list(source.get('table_cells')), inside_table=True)
    candidate_table = _block_inventory(_as_list(candidate.get('table_cells')), inside_table=True)

    source_outside_hashes = {item.get('text_hash') for item in source_outside.values() if item.get('text_hash')}
    source_table_hashes = {item.get('text_hash') for item in source_table.values() if item.get('text_hash')}
    candidate_outside_hashes = {item.get('text_hash') for item in candidate_outside.values() if item.get('text_hash')}
    candidate_table_hashes = {item.get('text_hash') for item in candidate_table.values() if item.get('text_hash')}
    moved_to_table = source_outside_hashes & candidate_table_hashes
    moved_outside = source_table_hashes & candidate_outside_hashes
    if moved_to_table or moved_outside:
        _add_issue(
            issues,
            severity='FAIL',
            category='inside_outside',
            risk_flag='inside_table_boundary_drift',
            message=f'inside-table/outside-table text boundary drift: outside→table={_join_values(moved_to_table)} table→outside={_join_values(moved_outside)}',
            source={'outside_hashes': sorted(source_outside_hashes), 'table_hashes': sorted(source_table_hashes)},
            candidate={'outside_hashes': sorted(candidate_outside_hashes), 'table_hashes': sorted(candidate_table_hashes)},
        )

    for key in sorted(set(source_outside) & set(candidate_outside)):
        if _as_dict(source_outside[key].get('style')) != _as_dict(candidate_outside[key].get('style')):
            _add_issue(
                issues,
                severity='WARN',
                category='inside_outside',
                risk_flag='outside_table_style_drift',
                message=f'outside-table style drift at {key}',
                source=source_outside[key].get('style'),
                candidate=candidate_outside[key].get('style'),
            )


def _diff_controls(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    issues: list[dict[str, Any]],
    *,
    informational_changes: list[dict[str, Any]] | None = None,
) -> None:
    source_controls = _control_inventory(_as_list(source.get('controls')))
    candidate_controls = _control_inventory(_as_list(candidate.get('controls')))
    source_keys = set(source_controls)
    candidate_keys = set(candidate_controls)
    if source_keys != candidate_keys:
        _add_issue(
            issues,
            severity='WARN',
            category='controls',
            risk_flag='control_anchor_drift',
            message=f'controls/images identity drift: missing={_join_values(source_keys - candidate_keys)} added={_join_values(candidate_keys - source_keys)}',
            source=sorted(source_keys),
            candidate=sorted(candidate_keys),
        )
    for key in sorted(source_keys & candidate_keys):
        src = source_controls[key]
        cand = candidate_controls[key]
        src_anchor = {name: src.get(name) for name in ('target_id', 'type', 'page', 'anchor')}
        cand_anchor = {name: cand.get(name) for name in ('target_id', 'type', 'page', 'anchor')}
        if src_anchor != cand_anchor:
            severity = 'WARN'
            _add_issue(
                issues,
                severity=severity,
                category='controls',
                risk_flag='control_anchor_drift',
                message=f'control/image anchor or proof drift at {key}',
                source=src,
                candidate=cand,
            )
        elif src.get('proof_hash') != cand.get('proof_hash') and informational_changes is not None:
            informational_changes.append(
                {
                    'category': 'controls',
                    'risk_flag': 'control_render_hash_changed',
                    'message': f'control proof hash changed but identity/type/page/anchor stayed stable at {key}',
                    'source': src,
                    'candidate': cand,
                }
            )


def _evidence_warnings(source: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    for label, payload in (('source', source), ('candidate', candidate)):
        structure = _as_dict(payload.get('structure_summary'))
        if structure.get('broad_text_block_enumeration_available') is False:
            warnings.append(f'{label} readback has current-block-only outside/table text arrays; use document_text_summary/raw artifacts for broad text evidence')
        caps = _as_dict(payload.get('caps'))
        if caps.get('truncated'):
            warnings.append(f'{label} readback arrays were already truncated before diff')
        if payload.get('read_only') is not True:
            warnings.append(f'{label} payload is not marked read_only')
    return warnings


def _write_artifact(artifact_dir: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'readback-diff-full-{int(time.time() * 1000)}.json'
    data = json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    path.write_bytes(data)
    return {'path': str(path), 'sha256': _hash_bytes(data), 'bytes': len(data)}


def summarize_readback_diff(
    source_payload: Mapping[str, Any] | None,
    candidate_payload: Mapping[str, Any] | None,
    *,
    artifact_dir: str | Path | None = None,
    max_issues: int = 20,
) -> dict[str, Any]:
    """Compare two readback manifests and return compact PASS/WARN/FAIL JSON.

    The diff is read-only diagnostic evidence. It never recommends XML/package
    mutation as a production HWP/HWPX repair path.
    """

    source = _normalize(source_payload)
    candidate = _normalize(candidate_payload)
    issues: list[dict[str, Any]] = []
    informational_changes: list[dict[str, Any]] = []
    warnings = _evidence_warnings(source, candidate)
    current_block_only_note = _current_block_only_diff_note(source, candidate)

    if current_block_only_note:
        warnings.append(current_block_only_note)
    else:
        _diff_style_summary(source, candidate, issues)
        _diff_table_cells(source, candidate, issues)
        _diff_inside_outside(source, candidate, issues)
    _diff_structure_counts(source, candidate, issues)
    _diff_controls(source, candidate, issues, informational_changes=informational_changes)

    risk_flags = []
    for issue in issues:
        flag = str(issue.get('risk_flag') or issue.get('category') or 'readback_drift')
        if flag not in risk_flags:
            risk_flags.append(flag)

    verdict = _severity_from_issues(issues)
    issue_count = len(issues)
    max_issues = max(1, int(max_issues or 20))
    compact_issues = issues[:max_issues]
    truncated = issue_count > max_issues

    raw_detail = {
        'source': source,
        'candidate': candidate,
        'issues': issues,
        'informational_changes': informational_changes,
        'risk_flags': risk_flags,
        'warnings': warnings,
    }
    raw_detail_bytes = len(json.dumps(_jsonable(raw_detail), ensure_ascii=False, sort_keys=True).encode('utf-8'))
    artifacts: list[dict[str, Any]] = []
    if artifact_dir is not None and (truncated or raw_detail_bytes > 50_000):
        artifacts.append(_write_artifact(artifact_dir, raw_detail))

    source_doc = _as_dict(source.get('document'))
    candidate_doc = _as_dict(candidate.get('document'))
    summary = f'readback diff: {verdict} issues={issue_count} source={source_doc.get("name") or "source"} candidate={candidate_doc.get("name") or "candidate"}'
    return {
        'schema_version': 'local-output-parser/readback-diff/v1',
        'ok': verdict != 'FAIL',
        'verdict': verdict,
        'summary': summary,
        'read_only_diagnostic': True,
        'production_note': 'Read-only diagnostic evidence only; do not use XML/package mutation as a production HWP/HWPX repair path.',
        'source': {
            'document': _jsonable(source_doc),
            'scope': source.get('scope'),
            'style_summary': _jsonable(_as_dict(source.get('style_summary'))),
            'structure_summary': _jsonable(_as_dict(source.get('structure_summary'))),
            'caps': _jsonable(_as_dict(source.get('caps'))),
            'input_path': source.get('input_path'),
            'input_sha256': source.get('input_sha256'),
        },
        'candidate': {
            'document': _jsonable(candidate_doc),
            'scope': candidate.get('scope'),
            'style_summary': _jsonable(_as_dict(candidate.get('style_summary'))),
            'structure_summary': _jsonable(_as_dict(candidate.get('structure_summary'))),
            'caps': _jsonable(_as_dict(candidate.get('caps'))),
            'input_path': candidate.get('input_path'),
            'input_sha256': candidate.get('input_sha256'),
        },
        'risk_flags': risk_flags,
        'issue_count': issue_count,
        'issues': _jsonable(compact_issues),
        'informational_changes': _jsonable(informational_changes[:20]),
        'warnings': warnings,
        'artifacts': artifacts,
        'caps': {
            'max_issues': max_issues,
            'truncated': truncated,
            'raw_detail_bytes': raw_detail_bytes,
            'full_detail_artifact_required': truncated or raw_detail_bytes > 50_000,
        },
    }


def format_readback_diff_human(payload: Mapping[str, Any] | None) -> str:
    diff = _as_dict(payload)
    if diff.get('schema_version') != 'local-output-parser/readback-diff/v1':
        return format_readback_diff_human(summarize_readback_diff(diff, diff))
    lines = [str(diff.get('summary') or f"readback diff: {diff.get('verdict') or 'UNKNOWN'}")]
    flags = [str(flag) for flag in _as_list(diff.get('risk_flags'))]
    lines.append(f"risk flags: {', '.join(flags) if flags else 'none'}")
    for issue in _as_list(diff.get('issues'))[:8]:
        item = _as_dict(issue)
        lines.append(f"- {item.get('severity') or 'WARN'} {item.get('category')}: {item.get('message')}")
    if _as_dict(diff.get('caps')).get('truncated'):
        lines.append('caps: compact issue list truncated; inspect artifact for full detail')
    for artifact in _as_list(diff.get('artifacts')):
        item = _as_dict(artifact)
        if item.get('path'):
            line = f"artifact: {item.get('path')}"
            if item.get('sha256'):
                line += f" sha256={item.get('sha256')}"
            lines.append(line)
    warnings = [str(warning) for warning in _as_list(diff.get('warnings'))]
    lines.append(f"warnings: {'; '.join(warnings) if warnings else 'none'}")
    lines.append(str(diff.get('production_note') or 'Read-only diagnostic evidence only; do not use XML/package mutation as a production HWP/HWPX repair path.'))
    return '\n'.join(lines)
