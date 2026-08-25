from __future__ import annotations

from typing import Any, Mapping


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    # `where` intentionally accepts no targeting fields. The local planner owns target choice;
    # the Windows side only reports the current live editor state.
    return step


def _style_summary(service: Any, hwp: Any) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    char_raw = service._style_parameter_snapshot(  # noqa: SLF001 - command package runtime is part of server internals.
        hwp,
        'CharShape',
        'HCharShape',
        ('Height', 'FaceNameHangul', 'FaceNameLatin', 'Bold', 'Italic', 'Underline'),
    )
    para_raw = service._style_parameter_snapshot(  # noqa: SLF001
        hwp,
        'ParagraphShape',
        'HParaShape',
        ('AlignType', 'LeftMargin', 'RightMargin', 'Indent', 'LineSpacing', 'LineSpacingType', 'PrevSpacing', 'NextSpacing'),
    )
    char_values = char_raw.get('values') if isinstance(char_raw.get('values'), dict) else {}
    para_values = para_raw.get('values') if isinstance(para_raw.get('values'), dict) else {}
    height = char_values.get('Height')
    font_size_pt = None
    if isinstance(height, (int, float)):
        font_size_pt = round(float(height) / 100.0, 2) if float(height) > 100 else float(height)
    if not char_raw.get('available'):
        warnings.append(str(char_raw.get('error') or 'character style unavailable'))
    if not para_raw.get('available'):
        warnings.append(str(para_raw.get('error') or 'paragraph style unavailable'))
    return {
        'character': {
            'font_size_pt': font_size_pt,
            'height_raw': height,
            'face_name': char_values.get('FaceNameHangul') or char_values.get('FaceNameLatin'),
            'bold': char_values.get('Bold'),
            'italic': char_values.get('Italic'),
            'underline': char_values.get('Underline'),
        },
        'paragraph': {
            'align': para_values.get('AlignType'),
            'left_margin': para_values.get('LeftMargin'),
            'right_margin': para_values.get('RightMargin'),
            'indent': para_values.get('Indent'),
            'line_spacing': para_values.get('LineSpacing'),
            'line_spacing_type': para_values.get('LineSpacingType'),
            'prev_spacing': para_values.get('PrevSpacing'),
            'next_spacing': para_values.get('NextSpacing'),
        },
        'raw_available': {
            'char_shape': bool(char_raw.get('available')),
            'para_shape': bool(para_raw.get('available')),
        },
    }, warnings


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    from app.local_cli_runtime import snapshot_live_location

    location = snapshot_live_location(
        hwp=handle.hwp,
        source_filename=handle.source_filename,
        working_copy_id=handle.session_id,
    )
    page_evidence = service._bundle_page_evidence(handle.hwp)  # noqa: SLF001
    style, warnings = _style_summary(service, handle.hwp)
    cursor = location.get('cursor') if isinstance(location.get('cursor'), dict) else {}
    nearby = location.get('nearby_context') if isinstance(location.get('nearby_context'), dict) else {}
    result = {
        'schema_version': manifest.get('version') or 'local-cli/where/v2-package',
        'read_only': True,
        'location': service._bundle_compact_location(location),  # noqa: SLF001
        'cursor': cursor,
        'page': {
            'current': page_evidence.get('page'),
            'method': page_evidence.get('method'),
            'page_count': location.get('page_count'),
            'evidence': page_evidence,
        },
        'selection': {
            'summary': location.get('selection_summary'),
            'mode': location.get('selection_mode'),
            'has_selection': cursor.get('has_selection'),
            'current_selected_ctrl': location.get('current_selected_ctrl'),
            'parent_ctrl': location.get('parent_ctrl'),
        },
        'table': {
            'in_cell': location.get('caret_in_table_cell'),
            'cell_ref': cursor.get('cell_ref'),
            'cell_addr': cursor.get('cell_addr'),
            'cur_field_state': location.get('cur_field_state'),
        },
        'context': {
            'current_paragraph_preview': location.get('current_paragraph_preview'),
            'nearby': nearby,
        },
        'style': style,
        'document': {
            'name': location.get('document_name'),
            'path': location.get('document_path'),
            'working_copy_id': location.get('working_copy_id'),
            'is_modified': location.get('document_is_modified'),
        },
    }
    return result, False, warnings
