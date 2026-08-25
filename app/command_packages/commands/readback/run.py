from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping

from app.command_packages.commands.context.run import _as_dict, _safe_text, _warn
from app.command_packages.commands.context.run import run_step as run_context_step
from app.command_packages.commands.selection_proof.run import run_step as run_selection_proof_step
from app.edit_ops import _get_document_text

_SCOPES = {'caret', 'selection', 'page', 'document'}
_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))
_IMAGE_LIKE_TYPES = {'pic', 'image', 'gso', 'shape', 'drawing', 'ole'}
_TABLE_LIKE_TYPES = {'tbl', 'table'}


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    scope = str(step.get('scope') or 'caret').strip().lower()
    if scope not in _SCOPES:
        raise error_type(f'command-bundle step {index} readback scope must be one of: {", ".join(sorted(_SCOPES))}')
    step['scope'] = scope
    for key in ('page_from', 'page_to', 'max_blocks', 'max_table_cells', 'max_controls'):
        if key not in step or step.get(key) in (None, ''):
            continue
        value = step.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise error_type(f'command-bundle step {index} readback {key} must be a positive integer')
    if step.get('page_from') is not None and step.get('page_to') is not None and int(step['page_to']) < int(step['page_from']):
        raise error_type(f'command-bundle step {index} readback page_to must be >= page_from')
    step['max_blocks'] = min(int(step.get('max_blocks') or 300), 1000)
    step['max_table_cells'] = min(int(step.get('max_table_cells') or 800), 3000)
    step['max_controls'] = min(int(step.get('max_controls') or 2048), 4096)
    return step


def _json_clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, tuple):
        return [_json_clean(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, max_chars=1200)
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    return _safe_text(value, max_chars=300)


def _hash_text(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()


def _hash_json(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8', errors='replace')
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _text_payload(text: Any, *, max_preview: int = 320) -> dict[str, Any]:
    if text in (None, ''):
        return {'preview': None, 'text_len': 0 if text == '' else None, 'normalized_hash': None}
    value = str(text)
    normalized = ' '.join(value.split())
    return {
        'preview': _safe_text(value, max_chars=max_preview),
        'text_len': len(value),
        'normalized_hash': _hash_text(normalized),
        'line_break_count': value.count('\n') + value.count('\r'),
    }


def _style_counts(style: Mapping[str, Any]) -> dict[str, Any]:
    character = _as_dict(style.get('character'))
    paragraph = _as_dict(style.get('paragraph'))
    font_family = character.get('face_name')
    font_size = character.get('font_size_pt')
    line_spacing = paragraph.get('line_spacing')
    align = paragraph.get('align')
    return {
        'font_family_counts': {str(font_family): 1} if font_family not in (None, '') else {},
        'font_size_pt_counts': {str(font_size): 1} if font_size not in (None, '') else {},
        'body_font_family_candidate': font_family,
        'body_font_size_pt_candidate': font_size,
        'alignment_counts': {str(align): 1} if align not in (None, '') else {},
        'line_spacing_counts': {str(line_spacing): 1} if line_spacing not in (None, '') else {},
        'mixed_font_warning': False,
        'mixed_size_warning': False,
        'raw_available': _as_dict(style.get('raw_available')),
    }


def _current_block_from_context(context: Mapping[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    block = _as_dict(context.get('block_context'))
    if not block:
        _warn(warnings, 'current block evidence unavailable from context readback')
        return None
    paragraph = _as_dict(context.get('paragraph_context'))
    line = _as_dict(context.get('line_context'))
    style = _as_dict(context.get('style_summary'))
    nearby = _as_dict(context.get('nearby_text'))
    inside_table = bool(block.get('inside_table'))
    cell = _as_dict(block.get('cell'))
    paragraph_index = block.get('paragraph_index')
    list_id = block.get('list_id')
    block_type = 'table_cell' if inside_table else 'paragraph'
    preview = paragraph.get('current_paragraph_text') or paragraph.get('current_paragraph_preview') or nearby.get('current')
    character = _as_dict(style.get('character'))
    para_style = _as_dict(style.get('paragraph'))
    item = {
        'block_id': f"{'table' if inside_table else 'body'}/{list_id if list_id is not None else 'unknown'}/{paragraph_index if paragraph_index is not None else 'unknown'}",
        'block_type': block_type,
        'inside_table': inside_table,
        'page_candidate': _as_dict(context.get('page')).get('current'),
        'location': {
            'list_id': list_id,
            'paragraph_index': paragraph_index,
            'paragraph_number_1based': paragraph.get('paragraph_number_1based'),
            'offset': block.get('offset'),
        },
        'table': None,
        'text': {
            **_text_payload(preview),
            'wrap_evidence': {
                'method': line.get('method'),
                'approximation': line.get('approximation'),
                'current_visual_line_preview': line.get('current_visual_line_preview'),
            },
        },
        'style': {
            'font_family': character.get('face_name'),
            'font_size_pt': character.get('font_size_pt'),
            'bold': character.get('bold'),
            'italic': character.get('italic'),
            'underline': character.get('underline'),
            'align': para_style.get('align'),
            'line_spacing': para_style.get('line_spacing'),
            'line_spacing_type': para_style.get('line_spacing_type'),
            'left_margin': para_style.get('left_margin'),
            'indent': para_style.get('indent'),
        },
        'context': {
            'previous': paragraph.get('previous_paragraph_preview') or nearby.get('before'),
            'current': paragraph.get('current_paragraph_preview') or nearby.get('current'),
            'next': paragraph.get('next_paragraph_preview') or nearby.get('after'),
            'heading_path': [],
        },
        'warnings': [],
    }
    if inside_table:
        item['table'] = {
            'cell_addr': cell.get('addr'),
            'row_1based': cell.get('row_1based'),
            'col_1based': cell.get('col_1based'),
            'row_index': cell.get('row_index'),
            'col_index': cell.get('col_index'),
            'field_name': block.get('field_name'),
        }
        if not cell.get('addr'):
            item['warnings'].append('caret is inside a table/cell but cell address is unavailable')
    return _json_clean(item)


def _document_text_summary(hwp: Any, warnings: list[str]) -> dict[str, Any]:
    try:
        text = _get_document_text(hwp)
    except Exception as exc:  # pragma: no cover - live Hancom runtime-specific.
        _warn(warnings, f'document text summary unavailable: {type(exc).__name__}: {exc}')
        return {'available': False, 'method': 'GetTextFile/get_text_file', 'error': f'{type(exc).__name__}: {exc}'}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    _warn(warnings, 'document text summary is flattened Hancom GetTextFile output; it is not used as outside_text_blocks because native table separation is not guaranteed')
    return {
        'available': True,
        'method': 'GetTextFile/get_text_file',
        'char_count': len(text),
        'nonempty_line_count': len(lines),
        'hash': _hash_text(text),
        'preview_lines': [_safe_text(line, max_chars=160) for line in lines[:8]],
        'table_separation': 'flattened_text_not_used_for_table_vs_outside_blocks',
    }


def _control_inventory(service: Any, hwp: Any, *, scope: str, step: Mapping[str, Any], page: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    if scope not in {'page', 'document'}:
        _warn(warnings, 'broad control/image inventory skipped for caret/selection scope; use --scope page or --scope document for control counts')
        return {'available': False, 'items': [], 'control_count_total': 0, 'control_count_returned': 0, 'scope': scope}
    inv_step: dict[str, Any] = {'max_controls': int(step.get('max_controls') or 2048)}
    if scope == 'page':
        page_from = step.get('page_from') or page.get('current')
        if page_from:
            inv_step['page_from'] = int(page_from)
            inv_step['page_to'] = int(step.get('page_to') or page_from)
        else:
            _warn(warnings, 'page-scoped control inventory requested but current page/page_from is unavailable; falling back to capped document control inventory')
    try:
        inventory = service._bundle_control_inventory(hwp, inv_step)  # noqa: SLF001 - readback composes read-only server primitive.
    except Exception as exc:  # pragma: no cover - live Hancom runtime-specific.
        _warn(warnings, f'control/image inventory unavailable: {type(exc).__name__}: {exc}')
        return {'available': False, 'items': [], 'control_count_total': 0, 'control_count_returned': 0, 'error': f'{type(exc).__name__}: {exc}'}
    for warning in inventory.get('warnings') or []:
        _warn(warnings, str(warning))
    result = dict(inventory)
    result['available'] = True
    return _json_clean(result)


def _control_type_key(item: Mapping[str, Any]) -> str:
    return str(item.get('type') or item.get('ctrl_id') or '').strip().lower()


def _structure_summary(*, controls: list[Any], current_block: Mapping[str, Any] | None, inventory: Mapping[str, Any], scope: str) -> dict[str, Any]:
    table_count = 0
    image_count = 0
    risk_flags: list[str] = []
    for raw in controls:
        item = _as_dict(raw)
        kind = _control_type_key(item)
        if kind in _TABLE_LIKE_TYPES or 'table' in kind:
            table_count += 1
        if kind in _IMAGE_LIKE_TYPES or 'pic' in kind or 'image' in kind:
            image_count += 1
    if current_block and current_block.get('inside_table') and table_count == 0:
        table_count = 1
        risk_flags.append('current_caret_inside_table_but_broad_table_inventory_unavailable')
    if inventory.get('control_count_total') not in (None, '') and inventory.get('control_count_returned') not in (None, ''):
        try:
            if int(inventory.get('control_count_total')) > int(inventory.get('control_count_returned')):
                risk_flags.append('control_inventory_truncated_or_filtered')
        except Exception:
            pass
    outside_text_block_count = 0 if current_block and current_block.get('inside_table') else (1 if current_block else 0)
    table_cell_count = 1 if current_block and current_block.get('inside_table') else 0
    result = {
        'outside_text_block_count': outside_text_block_count,
        'table_count': table_count,
        'table_cell_count': table_cell_count,
        'control_count': int(inventory.get('control_count_total') or len(controls) or 0),
        'control_count_returned': len(controls),
        'image_like_control_count': image_count,
        'page_break_or_blank_risks': [],
        'risk_flags': risk_flags,
    }
    if scope in {'page', 'document'}:
        risk_flags.append('broad_text_block_enumeration_current_block_only')
        result.update(
            {
                'broad_text_block_enumeration_available': False,
                'outside_text_blocks_scope': 'current_block_only',
                'table_cells_scope': 'current_block_only',
                'outside_text_block_count_partial': bool(outside_text_block_count),
                'table_cell_count_partial': bool(table_cell_count),
                'document_text_summary_scope': 'flattened_document_text',
            }
        )
    else:
        result.update(
            {
                'broad_text_block_enumeration_available': None,
                'outside_text_blocks_scope': scope,
                'table_cells_scope': scope,
            }
        )
    return result


def _write_raw_artifact(handle: Any, scope: str, payload: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    artifact_dir = handle.session_root / 'readback'
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f'readback-{scope}-{int(time.time() * 1000)}.json'
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    artifact_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > 250_000:
        _warn(warnings, 'raw readback artifact is large; CLI output remains capped and should reference the artifact path only')
    return {'path': str(artifact_path), 'sha256': f'sha256:{digest}', 'bytes': len(data)}


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    warnings: list[str] = []
    scope = str(step.get('scope') or 'caret').strip().lower()
    max_blocks = int(step.get('max_blocks') or 300)
    max_table_cells = int(step.get('max_table_cells') or 800)
    max_controls = int(step.get('max_controls') or 2048)

    context_result, _dirty, context_warnings = run_context_step(
        service=service,
        handle=handle,
        step={'op': 'context', 'label': 'readback:context'},
        binding=binding,
        manifest={'version': 'local-cli/context/v1-package'},
    )
    for warning in context_warnings or []:
        _warn(warnings, str(warning))
    for warning in context_result.get('warnings') or []:
        _warn(warnings, str(warning))

    selection_result: dict[str, Any] = {}
    if scope == 'selection':
        selection_result, _dirty, selection_warnings = run_selection_proof_step(
            service=service,
            handle=handle,
            step={'op': 'selection_proof', 'label': 'readback:selection-proof'},
            binding=binding,
            manifest={'version': 'local-cli/selection-proof/v1-package'},
        )
        for warning in selection_warnings or []:
            _warn(warnings, str(warning))
        for warning in selection_result.get('warnings') or []:
            _warn(warnings, str(warning))

    page = dict(_as_dict(context_result.get('page')))
    page_current = page.get('current')
    page_count = page.get('page_count')
    if scope == 'page':
        page_from = step.get('page_from') or page_current
        page_to = step.get('page_to') or page_from
        page['range'] = {'from': page_from, 'to': page_to, 'pages': [page_from] if page_from and page_from == page_to else None}
    elif scope == 'document':
        page['range'] = {'from': 1 if page_count else None, 'to': page_count, 'pages': None}
    elif step.get('page_from') or step.get('page_to'):
        page['range'] = {'from': step.get('page_from'), 'to': step.get('page_to') or step.get('page_from'), 'pages': None}

    current_block = _current_block_from_context(context_result, warnings)
    table_cells: list[dict[str, Any]] = []
    outside_blocks: list[dict[str, Any]] = []
    if current_block:
        if current_block.get('inside_table'):
            table_cells.append(current_block)
        else:
            outside_blocks.append(current_block)

    inventory = _control_inventory(service, handle.hwp, scope=scope, step=step, page=page, warnings=warnings)
    controls = list(inventory.get('items') or [])[:max_controls]
    document_text = _document_text_summary(handle.hwp, warnings) if scope in {'page', 'document'} else {'available': False, 'reason': 'not requested for caret/selection scope'}
    if scope in {'page', 'document'}:
        _warn(
            warnings,
            'page/document outside_text_blocks and table_cells are current-block-only; broad native table-separated text block enumeration is unavailable, so use document_text_summary and raw_artifact_path for broad text evidence',
        )
    style_summary = _style_counts(_as_dict(context_result.get('style_summary')))
    structure_summary = _structure_summary(controls=controls, current_block=current_block, inventory=inventory, scope=scope)

    selection_summary = {
        'from_context': _as_dict(context_result.get('structure_signals')).get('has_selection'),
        'selection_mode': _as_dict(context_result.get('structure_signals')).get('selection_mode'),
    }
    if selection_result:
        selection_summary.update(
            {
                'selection_state': selection_result.get('selection_state'),
                'selected_text': selection_result.get('selected_text'),
                'risk_flags': selection_result.get('risk_flags'),
                'boundary_context': selection_result.get('boundary_context'),
            }
        )

    raw_payload = {
        'schema_version': 'local-cli/readback-raw/v1',
        'scope': scope,
        'context': context_result,
        'selection_proof': selection_result or None,
        'control_inventory': inventory,
        'document_text_summary': document_text,
        'step': {key: step.get(key) for key in ('scope', 'page_from', 'page_to', 'max_blocks', 'max_table_cells', 'max_controls')},
    }
    artifact = _write_raw_artifact(handle, scope, raw_payload, warnings)

    if scope in {'page', 'document'} and not table_cells:
        _warn(warnings, 'native table cell text enumeration is currently limited to any current table cell; broad table cell extraction is unavailable')
    if not style_summary.get('font_family_counts'):
        _warn(warnings, 'font family unavailable from current Hancom style snapshot')
    if not style_summary.get('font_size_pt_counts'):
        _warn(warnings, 'font size unavailable from current Hancom style snapshot')

    broad_coverage = ' coverage=current-block-only' if scope in {'page', 'document'} else ''
    summary = (
        f"readback {scope}: page={page_current or 'unknown'} "
        f"outside_blocks={len(outside_blocks)} table_cells={len(table_cells)} "
        f"controls={structure_summary.get('control_count')}{broad_coverage} raw={artifact['path']}"
    )
    result = {
        'schema_version': manifest.get('version') or 'local-cli/readback/v1-package',
        'ok': True,
        'read_only': True,
        'scope': scope,
        'summary': summary,
        'document': {
            **_as_dict(context_result.get('document')),
            'page_count': page_count,
        },
        'page': page,
        'selection': selection_summary,
        'current_block': current_block,
        'outside_text_blocks': outside_blocks[:max_blocks],
        'table_cells': table_cells[:max_table_cells],
        'controls': controls,
        'style_summary': style_summary,
        'structure_summary': structure_summary,
        'document_text_summary': document_text,
        'caps': {
            'max_blocks': max_blocks,
            'max_table_cells': max_table_cells,
            'max_controls': max_controls,
            'truncated': len(outside_blocks) > max_blocks or len(table_cells) > max_table_cells or len(inventory.get('items') or []) > max_controls,
            'raw_artifact_path': artifact['path'],
            'raw_artifact_sha256': artifact['sha256'],
            'raw_artifact_bytes': artifact['bytes'],
        },
        'warnings': warnings,
    }
    return _json_clean(result), False, warnings
