from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import mimetypes
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]{1,120}\}\}")
_SUPPORTED_STATIC_EXTENSIONS = {'.hwpx'}
_ALLOWED_OUTPUT_EXTENSIONS = {'.hwp', '.hwpx', '.pdf'}
_RHWP_MODULE_CANDIDATES = ('rhwp',)
_RHWP_EXECUTABLE_CANDIDATES = ('rhwp', 'wasmtime', 'wasmer')


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    return str(value)


def _sha256_bytes(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _local_name(tag: str) -> str:
    if '}' in tag:
        return tag.rsplit('}', 1)[1]
    if ':' in tag:
        return tag.rsplit(':', 1)[1]
    return tag


def _parse_xml(data: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _iter_elements(root: ET.Element, *names: str) -> list[ET.Element]:
    wanted = {name.lower() for name in names}
    return [elem for elem in root.iter() if _local_name(elem.tag).lower() in wanted]


def _text_content(elem: ET.Element) -> str:
    return ''.join(part for part in elem.itertext() if part).strip()


def _compact_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def _classify_xml_entry(name: str) -> str:
    lower = name.lower()
    base = Path(lower).name
    if lower.startswith('contents/section') and base.endswith('.xml'):
        return 'section'
    if 'header' in base and base.endswith('.xml'):
        return 'header'
    if 'footer' in base and base.endswith('.xml'):
        return 'footer'
    if 'footnote' in base and base.endswith('.xml'):
        return 'footnote'
    if 'endnote' in base and base.endswith('.xml'):
        return 'endnote'
    return 'xml'


def _rhwp_adapter_metadata() -> dict[str, Any]:
    modules = {name: importlib.util.find_spec(name) is not None for name in _RHWP_MODULE_CANDIDATES}
    executables = {name: shutil.which(name) for name in _RHWP_EXECUTABLE_CANDIDATES}
    # A wasm runtime alone is not enough; a real rhwp module/CLI adapter must be
    # present before this path may inspect anything. Until then, fail closed so
    # HWPX package/XML output is never mislabeled as rhwp-derived evidence.
    adapter_available = bool(modules.get('rhwp') or executables.get('rhwp'))
    return {
        'name': 'rhwp-wasm',
        'read_only': True,
        'available': adapter_available,
        'status': 'available_not_integrated' if adapter_available else 'unavailable',
        'module_candidates': modules,
        'executable_candidates': {key: (str(value) if value else None) for key, value in executables.items()},
        'qa_claim_allowed': False,
        'not_final_qa_evidence': True,
    }


def _hwpx_xml_adapter_metadata() -> dict[str, Any]:
    return {
        'name': 'hwpx-package-xml',
        'read_only': True,
        'available': True,
        'status': 'available_secondary_static',
        'qa_claim_allowed': False,
        'not_final_qa_evidence': True,
    }


def _rhwp_unavailable_payload(path: Path, *, source_sha: str, ext: str) -> dict[str, Any]:
    adapter = _rhwp_adapter_metadata()
    if adapter['status'] == 'available_not_integrated':
        error = 'rhwp/WASM adapter appears present but is not integrated; refusing to fall back to HWPX package/XML under rhwp label'
    else:
        error = 'rhwp/WASM adapter unavailable; refusing to fall back to HWPX package/XML under rhwp label'
    return {
        'schema_version': 'local-cli/static-inspector/v1',
        'ok': False,
        'read_only': True,
        'authority': 'static_secondary',
        'authority_chain': ['secondary_diagnostic', 'static_secondary'],
        'qa_claim_allowed': False,
        'not_final_qa_evidence': True,
        'requires_hancom_native_corroboration': True,
        'engine_requested': 'rhwp',
        'engine': 'rhwp-wasm',
        'adapter': adapter,
        'document': {'path': str(path), 'extension': ext, 'sha256': source_sha},
        'error': error,
        'write_policy': {'direct_package_mutation_allowed': False, 'production_write_path': 'hancom_native_only'},
    }


def _table_cells(table: ET.Element) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for row_index, row in enumerate(_iter_elements(table, 'tr', 'row'), start=1):
        cells: list[dict[str, Any]] = []
        for col_index, cell in enumerate(_iter_elements(row, 'tc', 'cell'), start=1):
            # Only immediate-ish row descendants are not easy to distinguish with
            # ElementTree parentless traversal; for HWPX fixtures and most flat
            # tables this still gives stable read-only diagnostics.
            text = _compact_text(_text_content(cell))
            attrs = {str(key): str(value) for key, value in cell.attrib.items()}
            cells.append(
                {
                    'row': row_index,
                    'col': col_index,
                    'rowSpan': int(attrs.get('rowSpan') or attrs.get('rowspan') or attrs.get('rowCnt') or 1),
                    'colSpan': int(attrs.get('colSpan') or attrs.get('colspan') or attrs.get('colCnt') or 1),
                    'text': text,
                    'text_hash': _sha256_bytes(text.encode('utf-8')),
                    'attrs': attrs,
                }
            )
        if cells:
            rows.append(cells)
    return rows


def _markdown_preview(cells: list[list[dict[str, Any]]]) -> str:
    if not cells:
        return ''
    lines: list[str] = []
    for row_index, row in enumerate(cells):
        values = [str(cell.get('text') or '').replace('|', '\\|') for cell in row]
        lines.append('| ' + ' | '.join(values) + ' |')
        if row_index == 0:
            lines.append('| ' + ' | '.join('---' for _ in values) + ' |')
    return '\n'.join(lines)


def _extract_placeholders(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text):
        value = match.group(0)
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _field_attrs(root: ET.Element) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for elem in root.iter():
        lname = _local_name(elem.tag).lower()
        attrs = {str(key): str(value) for key, value in elem.attrib.items()}
        interesting = {
            key: value
            for key, value in attrs.items()
            if 'field' in key.lower() or key.lower() in {'name', 'fieldname', 'field_name'}
        }
        if lname.startswith('field') or interesting:
            result.append({'tag': lname, 'attrs': interesting or attrs, 'text': _compact_text(_text_content(elem))})
    return result


def _write_json_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding='utf-8')


def inspect_static(
    file_path: str | Path,
    *,
    artifact_dir: str | Path | None = None,
    extract_images: bool = False,
    engine: str = 'auto',
    max_text_chars: int = 20000,
) -> dict[str, Any]:
    """Read-only HWPX package/static inspector.

    This is deliberately secondary evidence. It never edits/repackages the
    source and cannot by itself authorize production QA PASS.
    """

    path = Path(file_path).expanduser().resolve()
    ext = path.suffix.lower()
    if not path.exists() or not path.is_file():
        return {
            'schema_version': 'local-cli/static-inspector/v1',
            'ok': False,
            'read_only': True,
            'authority': 'static_secondary',
            'authority_chain': ['secondary_diagnostic', 'static_secondary'],
            'qa_claim_allowed': False,
            'not_final_qa_evidence': True,
            'engine_requested': engine,
            'engine': 'rhwp-wasm' if engine == 'rhwp' else 'hwpx-package-xml',
            'adapter': _rhwp_adapter_metadata() if engine == 'rhwp' else _hwpx_xml_adapter_metadata(),
            'error': f'file not found: {path}',
        }
    source_sha = _sha256_file(path)
    if engine == 'rhwp':
        return _rhwp_unavailable_payload(path, source_sha=source_sha, ext=ext)
    if ext not in _SUPPORTED_STATIC_EXTENSIONS or not zipfile.is_zipfile(path):
        return {
            'schema_version': 'local-cli/static-inspector/v1',
            'ok': False,
            'read_only': True,
            'authority': 'static_secondary',
            'authority_chain': ['secondary_diagnostic', 'static_secondary'],
            'qa_claim_allowed': False,
            'not_final_qa_evidence': True,
            'engine_requested': engine,
            'engine': 'hwpx-package-xml' if engine in {'auto', 'hwpx-package-xml'} else engine,
            'adapter': _hwpx_xml_adapter_metadata() if engine in {'auto', 'hwpx-package-xml'} else {'name': engine, 'read_only': True, 'available': False, 'status': 'unsupported'},
            'document': {'path': str(path), 'extension': ext, 'sha256': source_sha},
            'error': 'static package inspector currently supports read-only .hwpx ZIP packages only',
        }

    artifact_root = Path(artifact_dir).expanduser().resolve() if artifact_dir else None
    image_artifact_dir = artifact_root / 'images' if artifact_root and extract_images else None
    if image_artifact_dir:
        image_artifact_dir.mkdir(parents=True, exist_ok=True)

    package_entries: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []
    footers: list[dict[str, Any]] = []
    footnotes: list[dict[str, Any]] = []
    endnotes: list[dict[str, Any]] = []
    equations: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    all_text_parts: list[str] = []

    with zipfile.ZipFile(path, 'r') as zf:
        for info in sorted(zf.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            name = info.filename
            data = zf.read(info)
            package_entries.append({'path': name, 'byte_length': len(data), 'sha256': _sha256_bytes(data)})
            lower = name.lower()
            if lower.startswith('bindata/'):
                mime, _encoding = mimetypes.guess_type(name)
                image_payload: dict[str, Any] = {
                    'id': f'image-{len(images)+1:04d}',
                    'source_path': name,
                    'extension': Path(name).suffix.lower(),
                    'mime': mime or 'application/octet-stream',
                    'byte_length': len(data),
                    'sha256': _sha256_bytes(data),
                    'read_only': True,
                    'ocr_available': False,
                }
                if image_artifact_dir:
                    out_name = f"{image_payload['id']}{Path(name).suffix.lower() or '.bin'}"
                    out_path = image_artifact_dir / out_name
                    out_path.write_bytes(data)
                    image_payload['extracted_path'] = str(out_path)
                images.append(image_payload)
                controls.append(
                    {
                        'target_id': image_payload['id'],
                        'type': 'bindata-image',
                        'source_path': name,
                        'proof_hash': image_payload['sha256'],
                        'page_candidate': None,
                    }
                )
                continue
            if not lower.endswith('.xml'):
                continue
            root = _parse_xml(data)
            if root is None:
                continue
            kind = _classify_xml_entry(name)
            text = _compact_text(_text_content(root))
            if text:
                all_text_parts.append(text)
            fields.extend(_field_attrs(root))
            if kind == 'section':
                section_index = len(sections)
                paragraphs = _iter_elements(root, 'p')
                sections.append({'index': section_index, 'path': name, 'paragraph_count': len(paragraphs), 'text': text[:max_text_chars]})
                for table_index, table in enumerate(_iter_elements(root, 'tbl', 'table'), start=1):
                    cells = _table_cells(table)
                    table_id = table.attrib.get('id') or table.attrib.get('name') or f'section{section_index}-table{table_index}'
                    payload = {
                        'id': str(table_id),
                        'section': section_index,
                        'path': name,
                        'rows': len(cells),
                        'cols': max((len(row) for row in cells), default=0),
                        'cells': cells,
                        'markdown_preview': _markdown_preview(cells),
                        'markdown_preview_only': True,
                        'requires_native_table_plan_for_insertion': True,
                        'production_insertion_allowed': False,
                    }
                    tables.append(payload)
                    controls.append({'target_id': str(table_id), 'type': 'table', 'source_path': name, 'proof_hash': _sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8'))})
                for script in _iter_elements(root, 'script', 'equation', 'eqedit'):
                    script_text = _compact_text(_text_content(script))
                    if script_text:
                        eq = {'id': f'equation-{len(equations)+1:04d}', 'section': section_index, 'path': name, 'script': script_text, 'authority': 'static_secondary'}
                        equations.append(eq)
                        controls.append({'target_id': eq['id'], 'type': 'equation', 'source_path': name, 'proof_hash': _sha256_bytes(script_text.encode('utf-8'))})
            elif kind == 'header':
                headers.append({'id': f'header-{len(headers)+1:04d}', 'path': name, 'text': text, 'authority': 'static_secondary'})
            elif kind == 'footer':
                footers.append({'id': f'footer-{len(footers)+1:04d}', 'path': name, 'text': text, 'authority': 'static_secondary'})
            elif kind == 'footnote':
                footnotes.append({'id': f'footnote-{len(footnotes)+1:04d}', 'path': name, 'text': text, 'authority': 'static_secondary'})
            elif kind == 'endnote':
                endnotes.append({'id': f'endnote-{len(endnotes)+1:04d}', 'path': name, 'text': text, 'authority': 'static_secondary'})

    full_text = '\n'.join(part for part in all_text_parts if part)
    placeholders = _extract_placeholders(full_text)
    field_names = []
    for item in fields:
        for value in item.get('attrs', {}).values():
            if value and value not in field_names:
                field_names.append(value)

    image_inventory = {
        'schema_version': 'local-cli/static-image-inventory/v1',
        'read_only': True,
        'authority': 'static_secondary',
        'qa_claim_allowed': False,
        'count': len(images),
        'hashes': [str(item.get('sha256')) for item in images if item.get('sha256')],
        'extracted_artifact_paths': [str(item.get('extracted_path')) for item in images if item.get('extracted_path')],
        'ocr_hook': {
            'available': False,
            'status': 'placeholder_only',
            'manifest_path': None,
            'required_when_raster_text_may_matter': True,
        },
    }
    readback_authority = {
        'primary_hancom_native_available': False,
        'primary_unavailable_reason': 'static inspector has no live Hancom/HWP API readback channel',
        'secondary_static_fields': ['headers', 'footers', 'footnotes', 'endnotes', 'equations', 'tables', 'images', 'controls'],
        'secondary_fields_final_qa_allowed': False,
        'required_primary_followup': 'Hancom-native readback/render remains required for final QA PASS',
    }

    payload = {
        'schema_version': 'local-cli/static-inspector/v1',
        'ok': True,
        'read_only': True,
        'authority': 'static_secondary',
        'authority_chain': ['secondary_diagnostic', 'static_secondary'],
        'qa_claim_allowed': False,
        'not_final_qa_evidence': True,
        'requires_hancom_native_corroboration': True,
        'engine_requested': engine,
        'engine': 'hwpx-package-xml',
        'adapter': _hwpx_xml_adapter_metadata(),
        'document': {
            'path': str(path),
            'name': path.name,
            'extension': ext,
            'sha256': source_sha,
            'package_entry_count': len(package_entries),
        },
        'structure_summary': {
            'section_count': len(sections),
            'paragraph_count': sum(int(item.get('paragraph_count') or 0) for item in sections),
            'table_count': len(tables),
            'image_count': len(images),
            'image_like_control_count': len(images),
            'control_count': len(controls),
            'header_count': len(headers),
            'footer_count': len(footers),
            'footnote_count': len(footnotes),
            'endnote_count': len(endnotes),
            'equation_count': len(equations),
        },
        'text': {'preview': full_text[:max_text_chars], 'text_len': len(full_text), 'sha256': _sha256_bytes(full_text.encode('utf-8'))},
        'sections': sections,
        'headers': headers,
        'footers': footers,
        'footnotes': footnotes,
        'endnotes': endnotes,
        'equations': equations,
        'tables': tables,
        'images': images,
        'image_inventory': image_inventory,
        'controls': controls,
        'fields': {'placeholders': placeholders, 'field_names': field_names, 'raw_field_nodes': fields[:200]},
        'package_entries': package_entries,
        'readback_authority': readback_authority,
        'write_policy': {'direct_package_mutation_allowed': False, 'production_write_path': 'hancom_native_only'},
        'warnings': ['static inspector is secondary diagnostic evidence; corroborate with Hancom-native readback/render before PASS'],
    }
    if artifact_root:
        manifest_path = artifact_root / 'static-inspector-manifest.json'
        _write_json_artifact(manifest_path, payload)
        payload['artifact_manifest_path'] = str(manifest_path)
    return payload


def compare_static(source_path: str | Path, candidate_path: str | Path, *, artifact_dir: str | Path | None = None) -> dict[str, Any]:
    source = inspect_static(source_path, artifact_dir=Path(artifact_dir) / 'source' if artifact_dir else None)
    candidate = inspect_static(candidate_path, artifact_dir=Path(artifact_dir) / 'candidate' if artifact_dir else None)
    issues: list[dict[str, Any]] = []
    for key in ('section_count', 'paragraph_count', 'table_count', 'image_count', 'control_count', 'header_count', 'footer_count', 'footnote_count', 'equation_count'):
        sv = source.get('structure_summary', {}).get(key)
        cv = candidate.get('structure_summary', {}).get(key)
        if sv != cv:
            issues.append({'severity': 'WARN', 'risk_flag': f'static_{key}_drift', 'message': f'static {key} differs', 'source': sv, 'candidate': cv})
    source_image_hashes = sorted(str(item.get('sha256')) for item in source.get('images', []) if isinstance(item, dict) and item.get('sha256'))
    candidate_image_hashes = sorted(str(item.get('sha256')) for item in candidate.get('images', []) if isinstance(item, dict) and item.get('sha256'))
    if source_image_hashes and candidate_image_hashes and source_image_hashes != candidate_image_hashes:
        issues.append(
            {
                'severity': 'WARN',
                'risk_flag': 'static_image_hash_drift',
                'message': 'static image/BinData hashes differ',
                'source_hashes': source_image_hashes,
                'candidate_hashes': candidate_image_hashes,
            }
        )
    if source.get('document', {}).get('sha256') == candidate.get('document', {}).get('sha256'):
        issues.append({'severity': 'WARN', 'risk_flag': 'static_same_package_hash', 'message': 'source and candidate package hashes are identical'})
    payload = {
        'schema_version': 'local-cli/static-compare/v1',
        'ok': bool(source.get('ok')) and bool(candidate.get('ok')),
        'read_only': True,
        'authority': 'static_secondary',
        'qa_claim_allowed': False,
        'not_final_qa_evidence': True,
        'source': source,
        'candidate': candidate,
        'issues': issues,
        'risk_flags': sorted({str(issue.get('risk_flag')) for issue in issues}),
        'verdict': 'REVIEW_REQUIRED_STATIC_NATIVE_CORROBORATION' if issues else 'STATIC_MATCH_SECONDARY_ONLY',
    }
    if artifact_dir:
        manifest_path = Path(artifact_dir).expanduser().resolve() / 'static-compare-manifest.json'
        _write_json_artifact(manifest_path, payload)
        payload['artifact_manifest_path'] = str(manifest_path)
    return payload


def build_field_fill_plan(file_path: str | Path, replacements: dict[str, Any], *, artifact_dir: str | Path | None = None) -> dict[str, Any]:
    static = inspect_static(file_path)
    placeholders = list(static.get('fields', {}).get('placeholders') or [])
    matched = []
    missing = []
    for key, value in replacements.items():
        target = str(key)
        entry = {'placeholder': target, 'replacement_preview': str(value)[:200], 'replacement_len': len(str(value))}
        if target in placeholders:
            matched.append(entry)
        else:
            missing.append(entry)
    payload = {
        'schema_version': 'local-cli/field-fill-plan/v1',
        'ok': True,
        'read_only': True,
        'authority': 'planning_only',
        'mutation_performed': False,
        'required_write_path': 'hancom_native_only',
        'production_ready_without_native_proof': False,
        'source': static.get('document'),
        'available_placeholders': placeholders,
        'matched_replacements': matched,
        'missing_replacements': missing,
        'native_command_family': 'field_or_exact_anchor_fill_via_hancom_native',
        'required_proof': ['pre-readback target identity', 'Hancom-native mutation result', 'post-readback target present/forbidden absent', 'rendered proof'],
        'warnings': ['plan only; no direct XML/ZIP replacement is permitted for production'],
    }
    if artifact_dir:
        manifest_path = Path(artifact_dir).expanduser().resolve() / 'field-fill-plan.json'
        _write_json_artifact(manifest_path, payload)
        payload['artifact_manifest_path'] = str(manifest_path)
    return payload


def output_format_policy(input_path: str | Path, output_path: str | Path, *, operation: str = 'save') -> dict[str, Any]:
    src = Path(input_path)
    dst = Path(output_path)
    src_ext = src.suffix.lower()
    dst_ext = dst.suffix.lower()
    risk_flags: list[str] = []
    if src_ext not in _ALLOWED_OUTPUT_EXTENSIONS:
        risk_flags.append('unsupported_input_extension')
    if dst_ext not in _ALLOWED_OUTPUT_EXTENSIONS:
        risk_flags.append('unsupported_output_extension')
    if src_ext in {'.hwp', '.hwpx'} and dst_ext in {'.hwp', '.hwpx'} and src_ext != dst_ext:
        risk_flags.append('cross_format')
    if operation in {'save', 'save-as'} and dst_ext == '.pdf':
        risk_flags.append('use_export_not_save')
    refused = bool(risk_flags)
    return {
        'schema_version': 'local-cli/output-format-policy/v1',
        'ok': True,
        'read_only': True,
        'authority': 'policy_only',
        'operation': operation,
        'input_extension': src_ext,
        'output_extension': dst_ext,
        'verdict': 'REFUSE' if refused else 'ALLOW_WITH_NATIVE_PROOF',
        'risk_flags': risk_flags,
        'direct_package_mutation_allowed': False,
        'requires_hancom_native_save_proof': not refused,
        'required_proof': ['Hancom-native save/export', 'reopen/readback', 'render proof'] if not refused else [],
    }


def quick_render_static(
    file_path: str | Path,
    *,
    output_path: str | Path,
    render_format: str = 'html',
    artifact_dir: str | Path | None = None,
) -> dict[str, Any]:
    static = inspect_static(file_path)
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    text = str(static.get('text', {}).get('preview') or '')
    if render_format == 'svg':
        lines = html.escape(text[:3000]).split('\n') or ['']
        body = ''.join(f'<text x="24" y="{40 + idx*20}" font-size="14">{line}</text>' for idx, line in enumerate(lines[:80]))
        content = f'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1400"><rect width="100%" height="100%" fill="white"/><text x="24" y="20" font-size="12" fill="red">NON-AUTHORITATIVE STATIC PREVIEW</text>{body}</svg>'
    else:
        table_previews = '\n'.join(str(table.get('markdown_preview') or '') for table in static.get('tables', []))
        content = '<!doctype html><meta charset="utf-8"><title>Static HWPX preview</title><body>'
        content += '<p style="color:#b00"><strong>NON-AUTHORITATIVE STATIC PREVIEW — Hancom render required for QA PASS.</strong></p>'
        content += '<pre>' + html.escape(text[:20000]) + '</pre>'
        if table_previews:
            content += '<h2>Tables markdown preview only</h2><pre>' + html.escape(table_previews) + '</pre>'
        content += '</body>'
    out.write_text(content, encoding='utf-8')
    payload = {
        'schema_version': 'local-cli/static-render/v1',
        'ok': True,
        'read_only': True,
        'authority': 'quick_render_secondary',
        'qa_claim_allowed': False,
        'not_final_qa_evidence': True,
        'risk_flags': ['non_authoritative'],
        'render_format': render_format,
        'output_path': str(out),
        'source': static.get('document'),
        'required_followup': 'Hancom-native render/contact-sheet proof before QA PASS',
    }
    if artifact_dir:
        manifest_path = Path(artifact_dir).expanduser().resolve() / 'static-render-manifest.json'
        _write_json_artifact(manifest_path, payload)
        payload['artifact_manifest_path'] = str(manifest_path)
    return payload


def load_replacements(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('replacements JSON must be an object')
    return data
