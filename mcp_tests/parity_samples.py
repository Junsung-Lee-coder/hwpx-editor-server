"""Test-owned request samples for backend-to-MCP parity checks.

Pure data: no app, MCP SDK or native import. The portable suite feeds every
sample through the backend's own command-package validators, and the MCP suite
sends the same samples through the real MCP transport to a fake backend.
"""
from __future__ import annotations

import hashlib

CTRL = 'ctrl/3/tbl/1'
HASH24 = 'sha256:' + 'a' * 24
SCOPED = {'page_from': 1, 'page_to': 1, 'target_id': CTRL, 'expected_hash': HASH24, 'expected_page': 1}


def sha(text: str) -> str:
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


_TABLE_CELLS = [['항목', '값'], ['A', '1']]
_FLAT = [value for row in _TABLE_CELLS for value in row]
_RANGE_CELLS = [['구분', '내용', '비고', '기한'], ['1', '가', '', '11/3']]
_RANGE_FLAT = [value for row in _RANGE_CELLS for value in row]

# Bundle operations that MCP must expose through hwpx_command and hwpx_bundle.
# Each list holds positive samples, without op/label.
BUNDLE_SAMPLES: dict[str, list[dict]] = {
    'context': [{}],
    'selection_proof': [{}],
    'where': [{}],
    'readback': [{'scope': 'document', 'max_blocks': 50}, {'scope': 'caret'}, {'scope': 'page', 'page_from': 1, 'page_to': 1}],
    'cell_format_exact': [
        {'section_anchor': '표 위 문장', **SCOPED, 'vertical_align': 'center', 'confirm_layout': True},
        {'section_anchor': '표 위 문장', **SCOPED, 'fill_color': '#FFF2CC', 'confirm_layout': True},
        {'section_anchor': '표 위 문장', **SCOPED, 'border': 'none', 'confirm_layout': True},
        {'section_anchor': '표 위 문장', 'around': '표 아래 문장', **SCOPED, 'cell_margin_mm': 2.0, 'confirm_layout': True},
    ],
    'control_inventory': [{'page_from': 1, 'page_to': 2, 'max_controls': 100}, {'section_anchor': '본문 기준 문장'}],
    'table_frame_inventory': [{'page_from': 1, 'page_to': 1}],
    'exact_control_select_proof': [dict(SCOPED)],
    'table_cell_structure_exact': [dict(SCOPED)],
    'control_delete_exact': [{**SCOPED, 'confirm_remove': True}],
    'control_move_resize_exact': [{**SCOPED, 'scale_percent': 80.0, 'confirm_layout': True},
                                  {**SCOPED, 'move_dx_mm': 5.0, 'move_dy_mm': -2.5, 'confirm_layout': True}],
    'control_join_previous_exact': [{'target_id': CTRL, 'expected_hash': HASH24, 'expected_page': 1, 'page_from': 1,
                                     'page_to': 1, 'delete_back_count': 1, 'max_page_after': 2, 'confirm_layout': True}],
    'cell_row_fit_exact': [{**SCOPED, 'row_height_mm': 12.0, 'confirm_layout': True},
                           {**SCOPED, 'resize_up_steps': 2, 'confirm_layout': True},
                           {**SCOPED, 'line_spacing': 120, 'confirm_layout': True}],
    'table_split_exact': [{**SCOPED, 'down_rows': 2, 'confirm_layout': True}],
    'table_structure_exact': [
        {**SCOPED, 'action': 'insert_row_below', 'row': 1, 'col': 1, 'count': 1, 'expected_rows': 2, 'expected_cols': 2, 'confirm_layout': True},
        {**SCOPED, 'action': 'merge_cells', 'row': 1, 'col': 1, 'end_row': 1, 'end_col': 2, 'expected_rows': 2, 'expected_cols': 2, 'confirm_layout': True},
        {**SCOPED, 'action': 'split_cell', 'row': 1, 'col': 1, 'split_rows': 1, 'split_cols': 2, 'expected_rows': 2, 'expected_cols': 2, 'confirm_layout': True},
    ],
    'native_table_insert': [{'rows': 2, 'cols': 2, 'cells': _TABLE_CELLS, 'field_name': 'parity_table',
                             'confirm_native_table': True, 'source_text_deleted': False,
                             'old_plain_text_removal': 'deferred_until_rendered_proof',
                             'flat_value_hashes': [sha(v) for v in _FLAT], 'non_empty_token_count': 4,
                             'non_empty_token_preview': _FLAT, 'next_proof_required': 'rendered proof'}],
    'anchor_range_replace_native_table': [{'section_anchor': '1. 일정', 'start_anchor': '구분|내용', 'end_before_anchor': '2. 다음',
                                           'caption_text': '표 1', 'rows': 2, 'cols': 4, 'cells': _RANGE_CELLS,
                                           'field_name': 'range_table', 'confirm_replace': True,
                                           'flat_value_hashes': [sha(v) for v in _RANGE_FLAT], 'non_empty_token_count': 7,
                                           'non_empty_token_preview': [v for v in _RANGE_FLAT if v.strip()][:8],
                                           'next_proof_required': 'rendered proof'}],
    'selected_text_delete_exact': [{'expected_text': '지울 원문', 'expected_hash': sha('지울 원문'), 'confirm_cleanup': True}],
    'paragraph_style_apply_exact': [{'match': '제목 문단', 'expected_page': 1, 'keep_with_next': True, 'widow_orphan': True,
                                     'pagebreak_before': 1, 'confirm_layout': True}],
    'paragraph_delete_exact': [{'match': '지울 문단', 'expected_page': 1, 'occurrence_on_page': 1,
                                'expected_previous_contains': '앞 문단', 'confirm_remove': True, 'max_page_after': 2}],
    'paragraph_join_previous_exact': [{'match': '둘째 줄', 'expected_page': 1, 'expected_previous_contains': '첫째',
                                       'delete_back_count': 1, 'max_page_after': 1, 'confirm_layout': True}],
    'paragraph_join_next_exact': [{'match': '첫째 줄', 'expected_page': 1, 'expected_next_contains': '둘째', 'delete_count': 1,
                                   'next_match': '둘째 줄', 'max_next_page_after': 1, 'insert_line_break': False,
                                   'move_to_line_end': True, 'confirm_layout': True}],
    'paragraph_rehome_exact': [{'delete_match': '옮길 문단', 'delete_expected_page': 1, 'insert_before_match': '기준 문단',
                                'insert_expected_page': 1, 'insert_text': '옮길 문단', 'expected_text_delta': 0, 'confirm_layout': True}],
    'anchor_insert': [{'target': '기준 문장', 'position': 'after-anchor', 'text': ' 추가'},
                      {'target': '기준 문장', 'position': 'before-heading', 'fragments': ['가', '나']}],
    'set_text_file': [{'text': '붙여 넣을 글', 'format': 'UNICODE', 'option': 'insertfile'}],
    'get_selected_text': [{'keep_select': True}],
    'style_inspect': [{'match': '제목', 'keep_position': True}],
    'typography_overview': [{'scope': 'document', 'max_samples': 20, 'max_sections': 5, 'max_styles': 10}],
    'layout_exact': [
        {'kind': 'page_setup', 'expected_pos': [0, 0, 0], 'landscape': True, 'margin_left_mm': 20.0,
         'apply_to': 'current_section', 'expected_before': {'landscape': False, 'margin_left_mm': 30.0}, 'confirm_layout': True},
        {'kind': 'columns', 'expected_pos': [0, 0, 0], 'count': 2, 'gap_mm': 8.0, 'same_width': True,
         'apply_to': 'current_section', 'expected_before': {'count': 1}, 'confirm_layout': True},
        {'kind': 'section_insert', 'expected_pos': [0, 3, 5], 'confirm_layout': True},
        {'kind': 'section_delete', 'expected_pos': [0, 7, 0], 'section_index': 2, 'confirm_layout': True},
        {'kind': 'hanging_indent', 'expected_pos': [0, 2, 3], 'marker_text': '1.', 'confirm_layout': True},
    ],
    'layout_inspect': [{}],
    'object_insert_exact': [
        {'kind': 'footnote', 'expected_pos': [0, 1, 4], 'text': '각주 내용', 'confirm_mutation': True},
        {'kind': 'endnote', 'expected_pos': [0, 1, 4], 'text': '미주 내용', 'confirm_mutation': True},
        {'kind': 'memo', 'expected_pos': [0, 1, 4], 'text': '메모', 'confirm_mutation': True},
        {'kind': 'hyperlink', 'expected_pos': [0, 1, 4], 'url': 'https://example.com/a', 'display_text': '링크', 'confirm_mutation': True},
        {'kind': 'bookmark', 'expected_pos': [0, 1, 4], 'name': 'parity_mark', 'confirm_mutation': True},
        {'kind': 'equation', 'expected_pos': [0, 1, 4], 'script': 'a over b', 'confirm_mutation': True},
        {'kind': 'line', 'expected_pos': [0, 1, 4], 'width_mm': 40.0, 'height_mm': 0.0, 'treat_as_char': True, 'confirm_mutation': True},
        {'kind': 'rectangle', 'expected_pos': [0, 1, 4], 'width_mm': 30.0, 'height_mm': 10.0, 'confirm_mutation': True},
        {'kind': 'ellipse', 'expected_pos': [0, 1, 4], 'width_mm': 20.0, 'height_mm': 20.0, 'treat_as_char': False, 'confirm_mutation': True},
        {'kind': 'header', 'expected_pos': [0, 0, 0], 'text': '머리말', 'apply_to': 'both', 'confirm_mutation': True},
        {'kind': 'footer', 'expected_pos': [0, 0, 0], 'text': '꼬리말', 'apply_to': 'odd', 'confirm_mutation': True},
    ],
    'hwp_action': [{'action_name': 'MoveDocBegin'}, {'action_name': 'TableCellBlock'}],
    'pyhwpx_call': [{'method_path': 'get_pos', 'args': [], 'kwargs': {}}, {'method_path': 'get_text_file', 'args': ['UNICODE', ''], 'kwargs': {}}],
    'page_number_apply': [{'position': 'bottom-center', 'start_number': 1, 'side_char': 'none', 'confirm_layout': True}],
    'official_numbering_apply': [{'match': '첫 문단', 'expected_page': 1, 'level': 1, 'confirm_layout': True}],
    'paragraph_insert_exact': [{'match': '기준', 'expected_page': 1, 'text': '새 문단', 'position': 'after', 'confirm_layout': True}],
    'paragraph_remove_exact': [{'match': '지울', 'expected_page': 1, 'expected_text': '지울 문단', 'confirm_remove': True}],
    'character_format_exact': [{'match': '굵게', 'expected_page': 1, 'bold': True, 'confirm_layout': True}],
    'paragraph_format_exact': [{'match': '가운데', 'expected_page': 1, 'align': 'center', 'confirm_layout': True}],
    'table_inventory': [{}],
    'table_read': [{'table_id': 'tbl:1011', 'max_cells': 100}],
    'table_edit_exact': [{'table_id': 'tbl:1011', 'expected_hash': 'sha256:' + 'b' * 24, 'expected_page': 1, 'expected_rows': 2,
                          'expected_cols': 2, 'action': 'insert_row_below', 'row': 1, 'confirm_layout': True}],
    'table_style_exact': [{'table_id': 'tbl:1011', 'expected_hash': 'sha256:' + 'b' * 24, 'expected_page': 1, 'expected_rows': 2,
                           'expected_cols': 2, 'scope': 'table', 'fill': '#FFFFFF', 'confirm_layout': True}],
    'table_width_exact': [{'table_id': 'tbl:1011', 'expected_hash': 'sha256:' + 'b' * 24, 'expected_page': 1, 'expected_rows': 2,
                           'expected_cols': 2, 'fit': 'text_width', 'confirm_layout': True}],
    'table_create_exact': [{'match': '기준', 'expected_page': 1, 'rows': 2, 'cols': 2, 'confirm_layout': True}],
    'text_replace_exact': [{'match': 'Annual Plan 2025', 'old_text': '2025', 'new_text': '2026', 'expected_page': 1, 'confirm_layout': True},
                           {'old_text': 'draft', 'new_text': 'final', 'occurrence': 2, 'expected_occurrences': 3, 'format_from': 'last_character', 'expected_page': 1, 'confirm_layout': True},
                           {'paragraph_index': 4, 'expected_text': 'Status: draft', 'old_text': 'draft', 'new_text': 'final', 'expected_page': 1, 'confirm_layout': True}],
    'header_footer_inventory': [{}],
    'header_footer_text_exact': [{'kind': 'header', 'section': 0, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'c' * 24, 'confirm_layout': True, 'old_text': 'SAMPLE', 'new_text': 'UPDATED'},
                                 {'kind': 'footer', 'section': 1, 'apply_to': 'odd', 'expected_area_hash': 'sha256:' + 'd' * 24, 'confirm_layout': True,
                                  'old_text': 'draft', 'new_text': 'final', 'occurrence': 2, 'expected_occurrences': 2, 'format_from': 'last_character'}],
    'header_footer_format_exact': [{'kind': 'header', 'section': 0, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'c' * 24, 'confirm_layout': True, 'match': 'REPORT', 'bold': True, 'text_color': '#C00000', 'align': 'center'},
                                   {'kind': 'header', 'section': 1, 'apply_to': 'even', 'expected_area_hash': 'sha256:' + 'e' * 24, 'confirm_layout': True,
                                    'paragraph_index': 0, 'expected_text': 'EVEN HEADER 2025', 'line_spacing_type': 'percent', 'line_spacing_value': 150}],
    'table_cell_text_exact': [{'table_id': 'tbl:1011', 'expected_hash': 'sha256:' + 'b' * 24, 'expected_page': 1, 'expected_rows': 3, 'expected_cols': 3,
                               'cells': [{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}, {'address': 'C1', 'expected_text': '', 'text': 'Filled'}], 'confirm_layout': True}],
}

# Requests MCP must refuse before any backend call: allowlists and typed limits.
BUNDLE_REFUSALS: dict[str, list[dict]] = {
    'hwp_action': [{'action_name': 'Delete'}, {'action_name': 'FileSaveAs'}, {'action_name': 'Erase'}],
    'pyhwpx_call': [{'method_path': 'Run'}, {'method_path': 'HAction.Run', 'args': ['Delete']}, {'method_path': 'save_as', 'args': ['x']},
                    {'method_path': 'get_pos', 'args': [], 'kwargs': {'__class__': 1}}],
    'cell_format_exact': [{'section_anchor': 'x', **SCOPED, 'border': 'solid', 'confirm_layout': True},
                          {'section_anchor': 'x', **SCOPED, 'fill_color': 'red', 'confirm_layout': True}],
    'cell_row_fit_exact': [{**SCOPED, 'row_height_mm': 12.0, 'resize_up_steps': 1, 'confirm_layout': True}],
    'control_delete_exact': [{**SCOPED, 'target_id': 'tbl:1', 'confirm_remove': True}, {**SCOPED, 'confirm_remove': False}],
    'layout_exact': [{'kind': 'rotate', 'expected_pos': [0, 0, 0], 'confirm_layout': True},
                     {'kind': 'columns', 'expected_pos': [0, 0, 0], 'count': 11, 'confirm_layout': True}],
    'object_insert_exact': [{'kind': 'picture', 'expected_pos': [0, 0, 0], 'confirm_mutation': True},
                            {'kind': 'hyperlink', 'expected_pos': [0, 0, 0], 'url': 'javascript:alert(1)', 'display_text': 'x', 'confirm_mutation': True}],
    'set_text_file': [{'text': 'x', 'format': 'HWPML2X', 'option': 'insertfile'}],
    # Table-only legacy engines fail on a non-table control with a runtime error, which holds the session
    # (found on Hancom 2024 with a section-definition target); MCP refuses such a target before the backend.
    'table_cell_structure_exact': [{**SCOPED, 'target_id': 'ctrl/0/secd/no-inst'}],
    'table_split_exact': [{**SCOPED, 'target_id': 'ctrl/7/gso/1', 'down_rows': 1, 'confirm_layout': True}],
}
for _op, _fields in (('cell_row_fit_exact', {'row_height_mm': 12.0, 'confirm_layout': True}),
                     ('table_structure_exact', {'action': 'insert_row_below', 'row': 1, 'col': 1, 'expected_rows': 2, 'expected_cols': 2, 'confirm_layout': True}),
                     ('cell_format_exact', {'section_anchor': 'x', 'vertical_align': 'top', 'confirm_layout': True})):
    BUNDLE_REFUSALS.setdefault(_op, []).append({**SCOPED, 'target_id': 'ctrl/0/secd/no-inst', **_fields})

# Direct (non-bundle) live-session REST routes exposed through hwpx_command.
# Each value: (MCP op, sample request, backend path).
REST_SAMPLES: dict[str, tuple[dict, str]] = {
    'target_info': ({'target': '기준 문장'}, '/local-cli/info'),
    'move_to_target': ({'target': '기준 문장'}, '/local-cli/move'),
    'select_target': ({'target': '기준 문장'}, '/local-cli/select'),
    'cell_info': ({}, '/local-cli/cell'),
    'cell_move': ({'direction': 'right', 'count': 1}, '/local-cli/cellmove'),
    'cursor_move': ({'direction': 'down', 'count': 2}, '/local-cli/cursormove'),
    'type_text': ({'text': '입력', 'allow_insert_at_caret': True}, '/local-cli/type'),
    'figure_section': ({'target_heading': '1. 개요', 'heading': '그림 절', 'intro': '소개', 'caption': '그림 1', 'body': '본문'}, '/local-cli/figure-section'),
    'cell_replace': ({'anchor': '표 위 문장', 'cell': 'B2', 'text': '새 값', 'expect_cell': 'B2', 'expect_old': '옛 값', 'expected_page': 1}, '/local-cli/cell-replace'),
    'font_size': ({'size_pt': 12.5, 'at_caret': False}, '/local-cli/fontsize'),
    'bold': ({'enabled': True}, '/local-cli/bold'),
    'font_family': ({'face_name': '맑은 고딕', 'at_caret': True}, '/local-cli/font'),
    'bullet': ({'text': '글머리 항목'}, '/local-cli/bullet'),
    'table_insert': ({'rows': 2, 'cols': 3}, '/local-cli/table'),
    'list_items': ({'count': 1}, '/local-cli/list'),
    'numbering_readback': ({}, '/local-cli/numbering-readback'),
}

# Queued edit-job operations (hwpx_job edit_and_convert), one positive sample each.
QUEUED_SAMPLES: dict[str, dict] = {
    'replace_all': {'find': '가', 'replace': '나', 'apply': 'all', 'match_case': True, 'whole_word': False},
    'replace_text_safe': {'find': '{{이름}}', 'replace': '홍길동', 'apply': 'first'},
    'replace_paragraph_safe': {'find': '옛 문단', 'replace': '새 문단', 'expected_present_after': '새 문단'},
    'paragraph_replace_native': {'find': '옛 문단', 'replace': '새 문단', 'expected_absent_after': '옛 문단'},
    'replace_paragraph_range_safe': {'start_find': '시작', 'end_find': '끝', 'replace': '대체'},
    'paragraph_range_replace_native': {'start_find': '시작', 'end_find': '끝', 'replace': '대체', 'allow_packed_paragraph': False},
    'replace_between_anchors_safe': {'start_anchor': '[', 'end_anchor': ']', 'replace': '값'},
    'clone_text_style': {'source_find': '원본', 'find': '대상'},
    'clone_paragraph_shape': {'source_find': '원본', 'find': '대상'},
    'clone_paragraph_layout': {'source_find': '원본', 'find': '대상'},
    'insert_after_text': {'find': '기준', 'insert': ' 뒤'},
    'insert_before_text': {'find': '기준', 'insert': '앞 '},
    'insert_at_document_end': {'insert': '마지막 문단', 'new_paragraph_before': True},
    'style_text': {'find': '강조', 'bold': True, 'text_color_rgb': '#C00000', 'height_pt': 12.0, 'underline': True, 'underline_shape': 1},
    'style_text_in_paragraph': {'paragraph_find': '문단', 'text': '단어', 'occurrence': 1, 'italic': True},
    'align_paragraph': {'find': '제목', 'align': 'center'},
    'paragraph_shape': {'find': '본문', 'line_spacing': 160, 'keep_with_next': True, 'left_margin': 10.0},
    'list_paragraph': {'find': '항목', 'kind': 'bullet', 'level': 1},
    'replace_empty_native_list_scaffold': {'cursor_pos': [0, 1, 0], 'items': [{'text': '첫째', 'kind': 'number', 'level': 1}, '둘째'],
                                           'kind': 'number', 'require_empty_scaffold': True},
    'cursor_replace_text': {'start_cursor_pos': [0, 1, 0], 'end_cursor_pos': [0, 1, 3], 'replace': '새', 'expected_selected_text': '옛것'},
    'cursor_delete_range': {'start_cursor_pos': [0, 1, 0], 'end_cursor_pos': [0, 1, 3], 'expected_selected_text': '지울'},
    'cursor_insert_text': {'cursor_pos': [0, 1, 0], 'insert': '삽입', 'delete_selection': False},
    'cursor_snapshot': {'find': '표', 'select_paragraph': True, 'capture_selected_text': True,
                        'expected_selected_text_present_any': ['표']},
    'control_delete_by_anchor': {'cursor_pos': [0, 2, 0], 'target_ctrl_id': 'gso', 'expected_cell_addr': 'A1', 'anchor_option': 1,
                                 'max_controls': 100, 'require_in_cell': True},
    'table_cell_action': {'cursor_pos': [3, 0, 0], 'action': 'block', 'expected_cell_addr': 'A1', 'expected_selection_mode': 3},
    'table_cell_clear_text': {'cursor_pos_from_snapshot': 'last', 'expected_cell_addr_from_snapshot': 'last'},
    'table_cell_replace_text': {'find': '셀 값', 'replace': '새 셀 값', 'expected_selected_text': '셀 값', 'select_paragraph': True},
    'table_patch_cells': {'entry_find': '표 머리', 'patches': [{'cell_addr': 'B2', 'replace': '값'}], 'expected_entry_cell_addr': 'A1'},
}

# Queued operations MCP must refuse before any backend call.
QUEUED_REFUSALS: dict[str, dict] = {
    'native_action': {'action': 'FileSaveAs', 'mode': 'execute', 'parameters': {'FileName': 'C:/x.hwpx'}},
    'page_number': {'position': 'bottom-center'},
    'official_numbering': {'find': '제목', 'level': 1},
}
