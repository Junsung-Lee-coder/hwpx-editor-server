from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_cli_v1.main import _print_command_payload, main as cli_main  # noqa: E402
from local_cli_v1.output_parser import (  # noqa: E402
    format_command_bundle_human,
    format_context_human,
    format_selection_proof_human,
    format_selected_text_proof_human,
    format_section_control_inventory_human,
    format_table_cell_structure_human,
    format_where_bundle_human,
    format_where_from_bundle,
    normalize_command_bundle,
    summarize_selection_proof,
    summarize_selected_text_proof,
    summarize_context,
    summarize_section_control_inventory,
    summarize_table_cell_structure,
)

def _sample_selected_response() -> dict[str, object]:
    return {
        'ok': True,
        'command': 'command-bundle',
        'summary': 'command-bundle succeeded: 3/3 step(s)',
        'dirty': False,
        'before': {
            'cursor_summary': 'pos 12',
            'selection_summary': 'selected 4 chars',
            'current_paragraph_preview': 'Before paragraph',
        },
        'after': {
            'cursor_summary': 'pos 12',
            'selection_summary': 'selected 4 chars',
            'current_paragraph_preview': 'After paragraph',
            'document_is_modified': False,
        },
        'steps': [
            {
                'index': 1,
                'label': 'where:before-selection-read',
                'op': 'where',
                'ok': True,
                'dirty': False,
                'result': {
                    'location': {
                        'cursor_summary': 'pos 12',
                        'selection_summary': 'selected 4 chars',
                        'current_paragraph_preview': 'Before paragraph',
                        'document_is_modified': False,
                    }
                },
                'future_server_field': 'preserved',
            },
            {
                'index': 2,
                'label': 'proof:selected-text',
                'op': 'get_selected_text',
                'ok': True,
                'dirty': False,
                'result': {
                    'schema_version': 'local-cli/selected-text-proof/v1',
                    'proof_method': 'pyhwpx.get_selected_text(keep_select=True)+restore_selected_range',
                    'keep_select_requested': True,
                    'text_len': 4,
                    'text_preview': '증빙텍스트',
                    'text_hash': 'sha256:sample',
                    'selected_text': '증빙텍스트',
                    'selected_text_normalized': '증빙텍스트',
                    'selection_source': 'active-selection',
                    'used_active_selection': True,
                    'used_cached_selection': False,
                    'cached_selection_available': True,
                    'cached_selected_text_hash': 'sha256:sample',
                    'selected_text_verified_against_cache': True,
                    'selected_range_before': [True, 0, 0, 1, 0, 0, 5],
                    'selected_range_after_read': [True, 0, 0, 1, 0, 0, 5],
                    'selected_range_restored': [True, 0, 0, 1, 0, 0, 5],
                    'has_active_selection_before': True,
                    'has_active_selection_after_read': True,
                    'has_active_selection_restored': True,
                    'selection_preserved_after_read': True,
                    'selection_restored': False,
                    'fail_closed_conditions': [
                        'restore failure when keep_select=true and an active pre-proof selection existed',
                        'empty selected text for selection-required mutations',
                    ],
                },
                'warnings': ['sample step warning'],
            },
            {
                'index': 3,
                'label': 'where:after-selection-read',
                'op': 'where',
                'ok': True,
                'dirty': False,
                'result': {
                    'location': {
                        'cursor_summary': 'pos 12',
                        'selection_summary': 'selected 4 chars',
                        'current_paragraph_preview': 'After paragraph',
                        'document_is_modified': False,
                    }
                },
            },
        ],
        'warnings': ['sample top warning'],
        'cursor_summary': 'pos 12',
        'selection_summary': 'selected 4 chars',
        'current_paragraph_preview': 'After paragraph',
        'unknown_top_field': {'kept': True},
    }


def _sample_where_response() -> dict[str, object]:
    return {
        'ok': True,
        'command': 'command-bundle',
        'summary': 'command-bundle succeeded: 1/1 step(s)',
        'dirty': False,
        'before': {
            'cursor_summary': 'pos 12',
            'selection_summary': 'none',
            'current_paragraph_preview': 'Before paragraph',
        },
        'after': {
            'cursor_summary': 'pos 12',
            'selection_summary': 'none',
            'current_paragraph_preview': 'Current paragraph',
            'caret_in_table_cell': False,
            'document_is_modified': False,
        },
        'steps': [
            {
                'index': 1,
                'label': 'where:current-location',
                'op': 'where',
                'ok': True,
                'dirty': False,
                'result': {
                    'location': {
                        'cursor_summary': 'pos 12',
                        'selection_summary': 'none',
                        'current_paragraph_preview': 'Current paragraph',
                        'caret_in_table_cell': False,
                        'document_is_modified': False,
                    }
                },
            }
        ],
        'cursor_summary': 'pos 12',
        'selection_summary': 'none',
        'current_paragraph_preview': 'Current paragraph',
    }


def _sample_context_response() -> dict[str, object]:
    return {
        'ok': True,
        'command': 'command-bundle',
        'summary': 'command-bundle succeeded: 1/1 step(s)',
        'dirty': False,
        'before': {'cursor_summary': 'pos 12', 'selection_summary': 'none'},
        'after': {
            'cursor_summary': 'pos 12',
            'selection_summary': 'none',
            'current_paragraph_preview': 'Current paragraph',
            'document_is_modified': False,
        },
        'steps': [
            {
                'index': 1,
                'label': 'context:edit-position',
                'op': 'context',
                'ok': True,
                'dirty': False,
                'result': {
                    'schema_version': 'local-cli/context/v1-package',
                    'label': 'context:edit-position',
                    'read_only': True,
                    'location': {
                        'cursor_summary': 'pos 12',
                        'selection_summary': 'none',
                        'current_paragraph_preview': 'Current paragraph',
                        'caret_in_table_cell': False,
                        'document_is_modified': False,
                    },
                    'page': {'current': 3, 'page_count': 7, 'method': 'KeyIndicator[3]'},
                    'current_cursor': {
                        'pos': [0, 12, 5],
                        'field_name': None,
                        'selection_mode': 0,
                        'is_cell': False,
                        'cell_addr': None,
                        'has_selection': False,
                    },
                    'block_context': {
                        'inside_table': False,
                        'current_block_type': 'paragraph',
                        'cell': None,
                        'list_id': 0,
                        'paragraph_index': 12,
                        'offset': 5,
                    },
                    'paragraph_context': {
                        'list_id': 0,
                        'paragraph_index': 12,
                        'paragraph_number_1based': 13,
                        'offset': 5,
                        'current_paragraph_preview': 'Current paragraph exact',
                        'method': 'save_position+select_text(current_paragraph)+get_selected_text+restore',
                        'approximation': False,
                        'warnings': [],
                    },
                    'line_context': {
                        'page_current': 3,
                        'page_count': 7,
                        'line_index': None,
                        'line_number': None,
                        'offset_in_paragraph': 5,
                        'method': 'save_position+MoveLineBegin+MoveSelLineEnd+get_selected_text+restore',
                        'approximation': False,
                        'warnings': [],
                        'current_visual_line_preview': 'Current visual line',
                    },
                    'selection_text_probes': {
                        'visual_line': {'available': True, 'text': 'Current visual line'},
                    },
                    'nearby_text': {
                        'before': 'Previous paragraph',
                        'current': 'Current paragraph',
                        'after': 'Next paragraph',
                    },
                    'style_summary': {
                        'character': {'font_size_pt': 10.0},
                        'paragraph': {},
                    },
                },
            }
        ],
    }


def _sample_selection_proof_response() -> dict[str, object]:
    return {
        'ok': True,
        'command': 'command-bundle',
        'summary': 'command-bundle succeeded: 1/1 step(s)',
        'dirty': False,
        'before': {
            'cursor_summary': 'pos 12',
            'selection_summary': 'selected 12 chars',
            'current_paragraph_preview': 'Visit https://example.com/path now',
        },
        'after': {
            'cursor_summary': 'pos 12',
            'selection_summary': 'selected 12 chars',
            'current_paragraph_preview': 'Visit https://example.com/path now',
            'document_is_modified': False,
        },
        'steps': [
            {
                'index': 1,
                'label': 'selection-proof:active-selection',
                'op': 'selection_proof',
                'ok': True,
                'dirty': False,
                'result': {
                    'schema_version': 'local-cli/selection-proof/v1-package',
                    'read_only': True,
                    'label': 'selection-proof:active-selection',
                    'selection_state': {
                        'has_selection': True,
                        'selection_mode': 1,
                        'selected_pos': {
                            'raw': [True, 0, 12, 14, 0, 12, 25],
                            'has_selection': True,
                            'available': True,
                        },
                        'position_before': [0, 12, 14],
                        'position_after': [0, 12, 14],
                    },
                    'selected_text': {
                        'text': 'example.com',
                        'preview': 'example.com',
                        'len': 11,
                        'is_null': False,
                        'is_empty': False,
                        'hash': 'sha256:sample-selection',
                        'method': 'pyhwpx.get_selected_text(keep_select=True)',
                    },
                    'boundary_context': {
                        'before_text': 'Visit https://',
                        'after_text': '/path now',
                        'before_char': '/',
                        'after_char': '/',
                        'paragraph_context': {
                            'paragraph_number_1based': 13,
                            'current_paragraph_preview': 'Visit https://example.com/path now',
                        },
                        'line_context': {
                            'current_visual_line_preview': 'Visit https://example.com/path now',
                        },
                    },
                    'risk_flags': {
                        'empty_selection': False,
                        'multi_paragraph_selection': False,
                        'starts_or_ends_inside_url_like_token': True,
                        'touches_url_or_doi_like_token': True,
                    },
                    'restore_evidence': {
                        'final_restore': {
                            'restored': True,
                            'strategy': 'select_text(saved_selected_pos)',
                        }
                    },
                },
            }
        ],
    }


def _load_selected_sample() -> dict[str, object]:
    return _sample_selected_response()


def _load_where_sample() -> dict[str, object]:
    return _sample_where_response()


def _load_context_sample() -> dict[str, object]:
    return _sample_context_response()


def _load_selection_proof_sample() -> dict[str, object]:
    return _sample_selection_proof_response()


def _run_cli(argv: list[str], response: dict[str, object]) -> tuple[int, str, str, dict[str, object]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    captured: dict[str, object] = {}

    def fake_post_json(base_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        captured['base_url'] = base_url
        captured['path'] = path
        captured['payload'] = payload
        return response

    with patch('local_cli_v1.main.post_json', fake_post_json), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli_main(argv)
    return rc, stdout.getvalue(), stderr.getvalue(), captured


def require_normalized_output() -> None:
    raw = _load_selected_sample()
    raw_before = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    normalized = normalize_command_bundle(raw)
    raw_after = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if raw_before != raw_after:
        raise SystemExit('normalize_command_bundle mutated the raw server response')
    if normalized['schema_version'] != 'local-output-parser/command-bundle/v1':
        raise SystemExit(f'unexpected schema: {normalized!r}')
    if normalized['completed_step_count'] != 3 or normalized['step_count'] != 3:
        raise SystemExit(f'unexpected step counts: {normalized!r}')
    if normalized['steps'][0].get('extras', {}).get('future_server_field') != 'preserved':
        raise SystemExit('unknown step fields were not preserved')
    if normalized.get('extras', {}).get('unknown_top_field', {}).get('kept') is not True:
        raise SystemExit('unknown top-level fields were not preserved')
    human = format_command_bundle_human(normalized)
    for needle in (
        'command-bundle succeeded: 3/3 step(s)',
        '1. where:before-selection-read: ok',
        '2. proof:selected-text: ok',
        '   text: 증빙텍스트',
        '   warning: sample step warning',
        'warning: sample top warning',
        'position: pos 12',
        'selection: selected 4 chars',
        'current: After paragraph',
    ):
        if needle not in human:
            raise SystemExit(f'human formatter missing {needle!r}:\n{human}')


def require_bundle_output_helpers() -> None:
    raw = _load_where_sample()
    where_payload = format_where_from_bundle(raw)
    if where_payload.get('parser_source') != 'command-bundle:where':
        raise SystemExit(f'where formatter did not mark source: {where_payload!r}')
    if where_payload.get('information_complete') is not True:
        raise SystemExit(f'where formatter lost useful position/selection proof: {where_payload!r}')
    where_human = format_where_bundle_human(raw)
    for needle in ('position: pos 12', 'selection: none', 'in cell: no', 'modified: no', 'current: Current paragraph'):
        if needle not in where_human:
            raise SystemExit(f'where human formatter missing {needle!r}:\n{where_human}')

    context_raw = _load_context_sample()
    context_payload = summarize_context(context_raw)
    if context_payload.get('parser_source') != 'command-bundle:context':
        raise SystemExit(f'context formatter did not mark source: {context_payload!r}')
    if context_payload.get('block_context', {}).get('current_block_type') != 'paragraph':
        raise SystemExit(f'context summary lost block classification: {context_payload!r}')
    if context_payload.get('nearby_text', {}).get('current') != 'Current paragraph':
        raise SystemExit(f'context summary lost nearby text: {context_payload!r}')
    if context_payload.get('paragraph_context', {}).get('paragraph_number_1based') != 13:
        raise SystemExit(f'context summary lost paragraph number: {context_payload!r}')
    if context_payload.get('line_context', {}).get('offset_in_paragraph') != 5:
        raise SystemExit(f'context summary lost line offset evidence: {context_payload!r}')
    if context_payload.get('selection_text_probes', {}).get('visual_line', {}).get('available') is not True:
        raise SystemExit(f'context summary lost exact text probe evidence: {context_payload!r}')
    context_human = format_context_human(context_raw)
    for needle in (
        'current edit position: pos 12',
        'page: 3 / 7',
        'block: paragraph',
        'paragraph: #13 (list=0, offset=5)',
        'line: visual line text available (page=3/7, paragraph offset=5)',
        'line current: Current visual line',
        'nearby current: Current paragraph',
        'style: font_size_pt=10.0',
    ):
        if needle not in context_human:
            raise SystemExit(f'context human formatter missing {needle!r}:\n{context_human}')
    if 'caret' in context_human.lower():
        raise SystemExit(f'context human formatter used forbidden wording:\n{context_human}')
    if 'caret' in json.dumps(context_payload, ensure_ascii=False).lower():
        raise SystemExit(f'context JSON formatter used forbidden wording:\n{context_payload!r}')

    selected_raw = _load_selected_sample()
    selected_payload = summarize_selected_text_proof(selected_raw)
    if selected_payload.get('selected_text_preview') != '증빙텍스트':
        raise SystemExit(f'selected-text summary lost text preview: {selected_payload!r}')
    if selected_payload.get('information_complete') is not True:
        raise SystemExit(f'selected-text summary lost proof fields: {selected_payload!r}')
    if selected_payload.get('selection_restored') is not False or not selected_payload.get('selected_range_restored'):
        raise SystemExit(f'selected-text summary lost selection restoration fields: {selected_payload!r}')
    if selected_payload.get('selection_source') != 'active-selection' or selected_payload.get('selected_text_verified_against_cache') is not True:
        raise SystemExit(f'selected-text summary lost cache/source proof fields: {selected_payload!r}')
    selected_human = format_selected_text_proof_human(selected_raw)
    for needle in ('text: 증빙텍스트', 'text hash: sha256:sample', 'proof method:', 'selection source: active-selection', 'cached proof verified: True', 'selected range:', 'selection restored: False', 'position: pos 12'):
        if needle not in selected_human:
            raise SystemExit(f'selected-text human formatter missing {needle!r}:\n{selected_human}')

    selection_proof_raw = _load_selection_proof_sample()
    selection_proof_payload = summarize_selection_proof(selection_proof_raw)
    if selection_proof_payload.get('schema_version') != 'local-output-parser/selection-proof/v1':
        raise SystemExit(f'selection-proof summary schema missing: {selection_proof_payload!r}')
    if selection_proof_payload.get('selected_text', {}).get('preview') != 'example.com':
        raise SystemExit(f'selection-proof summary lost selected text: {selection_proof_payload!r}')
    if selection_proof_payload.get('boundary_context', {}).get('before_char') != '/':
        raise SystemExit(f'selection-proof summary lost boundary char: {selection_proof_payload!r}')
    if selection_proof_payload.get('risk_flags', {}).get('starts_or_ends_inside_url_like_token') is not True:
        raise SystemExit(f'selection-proof summary lost URL boundary risk: {selection_proof_payload!r}')
    if selection_proof_payload.get('restore_evidence', {}).get('final_restore', {}).get('restored') is not True:
        raise SystemExit(f'selection-proof summary lost restore evidence: {selection_proof_payload!r}')
    selection_proof_human = format_selection_proof_human(selection_proof_raw)
    for needle in ('read-only: yes', 'has selection: True', 'text: example.com', 'boundary chars:', 'paragraph: #13', 'line current:', 'starts_or_ends_inside_url_like_token', 'restored: True'):
        if needle not in selection_proof_human:
            raise SystemExit(f'selection-proof human formatter missing {needle!r}:\n{selection_proof_human}')
    if 'caret' in selection_proof_human.lower():
        raise SystemExit(f'selection-proof human formatter used forbidden wording:\n{selection_proof_human}')

    inventory_raw = {
        'ok': True,
        'command': 'command-bundle',
        'steps': [
            {
                'index': 1,
                'label': 'inventory:section-controls',
                'op': 'control_inventory',
                'ok': True,
                'result': {
                    'read_only': True,
                    'scope': {'page_from': 19, 'page_to': 21},
                    'control_count_total': 2,
                    'control_count_returned': 1,
                    'items': [
                        {
                            'target_id': 'ctrl/0/gso/no-inst',
                            'page': 19,
                            'type': 'gso',
                            'proof_hash': 'sha256:sample',
                            'bounds': {'X': 10, 'Y': 20},
                            'text_preview': 'diagram',
                        }
                    ],
                },
            }
        ],
    }
    inventory_payload = summarize_section_control_inventory(inventory_raw)
    if inventory_payload.get('schema_version') != 'local-output-parser/section-control-inventory/v1':
        raise SystemExit(f'inventory summary schema missing: {inventory_payload!r}')
    if inventory_payload.get('control_count_returned') != 1:
        raise SystemExit(f'inventory summary lost counts: {inventory_payload!r}')
    inventory_human = format_section_control_inventory_human(inventory_raw)
    for needle in ('read-only: yes', 'ctrl/0/gso/no-inst page=19 type=gso hash=sha256:sample', 'bounds:'):
        if needle not in inventory_human:
            raise SystemExit(f'inventory human formatter missing {needle!r}:\n{inventory_human}')

    structure_raw = {
        'ok': True,
        'command': 'command-bundle',
        'steps': [
            {
                'index': 1,
                'label': 'probe:table-cell-structure-exact',
                'op': 'table_cell_structure_exact',
                'ok': True,
                'dirty': False,
                'result': {
                    'read_only': True,
                    'mutation': None,
                    'target_proof': {
                        'target_id': 'ctrl/99/tbl/2038321113',
                        'expected_hash': 'sha256:f56d0b58bc9520222cd6fabd',
                        'expected_page': 25,
                        'matched_before': {
                            'target_id': 'ctrl/99/tbl/2038321113',
                            'page': 25,
                            'type': 'tbl',
                            'proof_hash': 'sha256:f56d0b58bc9520222cd6fabd',
                        },
                    },
                    'enter': {'is_cell': True, 'normal_edit_state': True, 'cell_addr': 'A1'},
                    'entered_snapshot': {'selection_mode': 0, 'cell_addr': 'A1'},
                    'metrics': {
                        'cell_addr_str': {'value': 'A1'},
                        'cell_addr_tuple': {'value': [0, 0]},
                        'row_count': {'error': "AttributeError: 'NoneType' object has no attribute 'get'"},
                        'col_num': {'value': 1},
                        'table_width_mm': {'value': 168.01},
                        'table_height_mm': {'value': 341.21},
                        'col_width_mm': {'value': 168.01},
                        'row_height_mm': {'value': 341.21},
                    },
                    'navigation_summary': {
                        'any_navigation_moved': False,
                        'addresses_seen_zero_based_col_row': [[0, 0]],
                    },
                    'same_anchor_group': {
                        'control_ids': ['ctrl/99/tbl/2038321113', 'ctrl/100/gso/1248538063'],
                        'types': ['tbl', 'gso'],
                        'fit_risk': True,
                        'fit_note': 'table and graphic share one anchor',
                    },
                    'clipping_owner_hypothesis': 'single-cell-or-non-navigable table container/frame is the active limiter',
                    'next_safe_step_hint': 'Do not force TableSplitTable if navigation cannot leave A1.',
                },
            }
        ],
    }
    structure_payload = summarize_table_cell_structure(structure_raw)
    if structure_payload.get('schema_version') != 'local-output-parser/table-cell-structure-exact/v1':
        raise SystemExit(f'structure summary schema missing: {structure_payload!r}')
    if structure_payload.get('single_cell_evidence', {}).get('table_equals_cell_size') is not True:
        raise SystemExit(f'structure summary lost single-cell evidence: {structure_payload!r}')
    structure_human = format_table_cell_structure_human(structure_raw)
    for needle in (
        'read-only: yes',
        'target: ctrl/99/tbl/2038321113 page=25 type=tbl hash=sha256:f56d0b58bc9520222cd6fabd',
        'finding: single-cell/non-navigable table container evidence',
        'same-anchor: types=tbl, gso controls=ctrl/99/tbl/2038321113, ctrl/100/gso/1248538063',
        'next: Do not force TableSplitTable if navigation cannot leave A1.',
    ):
        if needle not in structure_human:
            raise SystemExit(f'structure human formatter missing {needle!r}:\n{structure_human}')


def require_cli_json_mode() -> None:
    raw = _load_selected_sample()
    rc, stdout, stderr, captured = _run_cli(['--base-url', 'http://sample.invalid', 'bundle-run', '--json', 'selected-text-proof'], raw)
    if rc != 0:
        raise SystemExit(f'bundle-run --json failed: stderr={stderr!r}')
    payload = json.loads(stdout)
    if payload.get('schema_version') != 'local-output-parser/command-bundle/v1':
        raise SystemExit(f'CLI --json did not print normalized parser JSON: {payload!r}')
    if 'bundle: selected-text-proof' in stdout:
        raise SystemExit('CLI --json should not include human bundle plan lines')
    if captured.get('path') != '/local-cli/command-bundle':
        raise SystemExit(f'CLI posted to wrong path: {captured!r}')
    request_payload = captured.get('payload') if isinstance(captured.get('payload'), dict) else {}
    if set(request_payload) != {'steps', 'session_id'}:
        raise SystemExit(f'bundle-run leaked local metadata to server payload: {request_payload!r}')

    rc, stdout, stderr, _captured = _run_cli(['--base-url', 'http://sample.invalid', 'bundle-run', 'selected-text-proof'], raw)
    if rc != 0:
        raise SystemExit(f'bundle-run human mode failed: stderr={stderr!r}')
    if 'bundle: selected-text-proof' not in stdout or 'text: 증빙텍스트' not in stdout:
        raise SystemExit(f'bundle-run human mode missed plan or parsed output:\n{stdout}')


def require_first_class_bundle_commands() -> None:
    select_active = {
        'ok': True,
        'summary': "selected match 1 for 'doi.org'",
        'selected_text': 'doi.org',
        'active_selection_verified': True,
        'safe_for_type': True,
        'selection_status': 'active',
    }
    rc, stdout, stderr, captured = _run_cli(['--base-url', 'http://sample.invalid', 'select', 'doi.org'], select_active)
    if rc != 0:
        raise SystemExit(f'select active command failed: stderr={stderr!r}')
    if captured.get('path') != '/local-cli/select':
        raise SystemExit(f'select did not use direct select route: {captured!r}')
    if 'selected text: doi.org' not in stdout:
        raise SystemExit(f'select active output missed selected text:\n{stdout}')

    select_degraded = {
        'ok': True,
        'summary': "found match 1 for 'doi.org', but active selection was not preserved",
        'selected_text': 'doi.org',
        'active_selection_verified': False,
        'safe_for_type': False,
        'selection_status': 'degraded',
        'warning': 'live Hancom get_selected_pos did not match the selected range after verification',
    }
    rc, stdout, stderr, _captured = _run_cli(['--base-url', 'http://sample.invalid', 'select', 'doi.org'], select_degraded)
    if rc != 0:
        raise SystemExit(f'select degraded command failed: stderr={stderr!r}')
    if 'selected text: doi.org' in stdout:
        raise SystemExit(f'select degraded output must not imply active selection:\n{stdout}')
    if 'cached selected-text proof only: doi.org' not in stdout or 'warning: live Hancom get_selected_pos' not in stdout:
        raise SystemExit(f'select degraded output missed cached-proof warning:\n{stdout}')

    where_raw = _load_where_sample()
    rc, stdout, stderr, captured = _run_cli(['--base-url', 'http://sample.invalid', 'where'], where_raw)
    if rc != 0:
        raise SystemExit(f'where command failed: stderr={stderr!r}')
    if captured.get('path') != '/local-cli/command-bundle':
        raise SystemExit(f'where did not use command-bundle: {captured!r}')
    request_payload = captured.get('payload') if isinstance(captured.get('payload'), dict) else {}
    if request_payload.get('steps') != [{'op': 'where', 'label': 'where:current-location'}]:
        raise SystemExit(f'where posted the wrong bundle: {request_payload!r}')
    for needle in ('position: pos 12', 'selection: none', 'current: Current paragraph'):
        if needle not in stdout:
            raise SystemExit(f'where output missing {needle!r}:\n{stdout}')

    context_raw = _load_context_sample()
    rc, stdout, stderr, captured = _run_cli(['--base-url', 'http://sample.invalid', 'context'], context_raw)
    if rc != 0:
        raise SystemExit(f'context command failed: stderr={stderr!r}')
    if captured.get('path') != '/local-cli/command-bundle':
        raise SystemExit(f'context did not use command-bundle: {captured!r}')
    request_payload = captured.get('payload') if isinstance(captured.get('payload'), dict) else {}
    if request_payload.get('steps') != [{'op': 'context', 'label': 'context:edit-position'}]:
        raise SystemExit(f'context posted the wrong bundle: {request_payload!r}')
    for needle in ('current edit position: pos 12', 'page: 3 / 7', 'block: paragraph', 'paragraph: #13', 'line: visual line text available', 'line current: Current visual line', 'nearby current: Current paragraph'):
        if needle not in stdout:
            raise SystemExit(f'context output missing {needle!r}:\n{stdout}')

    rc, stdout, stderr, _captured = _run_cli(['--base-url', 'http://sample.invalid', 'context', '--json'], context_raw)
    if rc != 0:
        raise SystemExit(f'context --json failed: stderr={stderr!r}')
    json_payload = json.loads(stdout)
    if json_payload.get('schema_version') != 'local-output-parser/context/v1':
        raise SystemExit(f'context --json printed wrong schema: {json_payload!r}')
    if json_payload.get('paragraph_context', {}).get('paragraph_number_1based') != 13:
        raise SystemExit(f'context --json missed paragraph context: {json_payload!r}')
    if json_payload.get('line_context', {}).get('line_number') is not None or json_payload.get('line_context', {}).get('approximation') is not False:
        raise SystemExit(f'context --json missed exact line text context: {json_payload!r}')
    if json_payload.get('selection_text_probes', {}).get('visual_line', {}).get('available') is not True:
        raise SystemExit(f'context --json missed selection text probes: {json_payload!r}')

    selected_raw = _load_selected_sample()
    rc, stdout, stderr, captured = _run_cli(
        ['--base-url', 'http://sample.invalid', 'selected-text-proof', '--clear-selection'],
        selected_raw,
    )
    if rc != 0:
        raise SystemExit(f'selected-text-proof command failed: stderr={stderr!r}')
    request_payload = captured.get('payload') if isinstance(captured.get('payload'), dict) else {}
    steps = request_payload.get('steps') if isinstance(request_payload.get('steps'), list) else []
    if [step.get('op') for step in steps] != ['where', 'get_selected_text', 'where']:
        raise SystemExit(f'selected-text-proof posted wrong ops: {request_payload!r}')
    if steps[1].get('keep_select') is not False:
        raise SystemExit(f'--clear-selection did not flip keep_select: {request_payload!r}')
    if 'text: 증빙텍스트' not in stdout or 'text hash: sha256:sample' not in stdout:
        raise SystemExit(f'selected-text-proof output missed parsed proof:\n{stdout}')

    selection_proof_raw = _load_selection_proof_sample()
    rc, stdout, stderr, captured = _run_cli(['--base-url', 'http://sample.invalid', 'selection-proof'], selection_proof_raw)
    if rc != 0:
        raise SystemExit(f'selection-proof command failed: stderr={stderr!r}')
    request_payload = captured.get('payload') if isinstance(captured.get('payload'), dict) else {}
    if request_payload.get('steps') != [{'op': 'selection_proof', 'label': 'selection-proof:active-selection'}]:
        raise SystemExit(f'selection-proof posted wrong bundle: {request_payload!r}')
    if 'text: example.com' not in stdout or 'risk flags:' not in stdout:
        raise SystemExit(f'selection-proof output missed parsed proof:\n{stdout}')

    rc, stdout, stderr, _captured = _run_cli(['--base-url', 'http://sample.invalid', 'selection-proof', '--json'], selection_proof_raw)
    if rc != 0:
        raise SystemExit(f'selection-proof --json failed: stderr={stderr!r}')
    json_payload = json.loads(stdout)
    if json_payload.get('schema_version') != 'local-output-parser/selection-proof/v1':
        raise SystemExit(f'selection-proof --json printed wrong schema: {json_payload!r}')
    if json_payload.get('risk_flags', {}).get('touches_url_or_doi_like_token') is not True:
        raise SystemExit(f'selection-proof --json missed risk flags: {json_payload!r}')


def require_generic_command_proof_output() -> None:
    payload = {
        'ok': True,
        'summary': 'typed 3 chars at the current caret position; after: after paragraph',
        'proof': {
            'operation': 'type',
            'scope': 'replace-selection',
            'before_has_selection': True,
            'after_has_selection': False,
            'before_selected_pos': [True, 0, 0, 1, 0, 0, 4],
            'after_selected_pos': [False, 0, 0, 4, 0, 0, 4],
            'selected_text_source': 'cached-selected-text-proof',
            'replaced_text_preview': 'old',
            'replaced_text_len': 3,
            'replaced_text_hash': 'hash-old',
            'inserted_text_len': 3,
            'inserted_text_hash': 'hash-new',
            'selection_cache_cleared': True,
            'method': 'Delete+insert_text',
            'after_paragraph_preview': 'after paragraph',
            'after_paragraph_hash': 'hash-after',
        },
    }
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        _print_command_payload(payload)
    human = stdout.getvalue()
    for needle in (
        'proof:',
        'operation=type',
        'scope=replace-selection',
        'replaced_text_hash=hash-old',
        'inserted_text_hash=hash-new',
        'before_selected_pos=[true,0,0,1,0,0,4]',
        'selection_cache_cleared=True',
        'after_paragraph_hash=hash-after',
    ):
        if needle not in human:
            raise SystemExit(f'generic proof output missing {needle!r}:\n{human}')

    fontsize_payload = {
        'ok': True,
        'summary': 'applied 12pt font size to the current selection',
        'proof': {
            'operation': 'fontsize',
            'scope': 'current selection',
            'style': {
                'requested_font_size_pt': 12.0,
                'applied_font_size_pt': 12.0,
                'strategy': 'hwp.set_font',
            },
            'method': 'hwp.set_font',
            'selection_cache_cleared': True,
        },
    }
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        _print_command_payload(fontsize_payload)
    human = stdout.getvalue()
    for needle in ('operation=fontsize', 'scope=current selection', 'style={requested_font_size_pt=12.0', 'selection_cache_cleared=True'):
        if needle not in human:
            raise SystemExit(f'fontsize proof output missing {needle!r}:\n{human}')


def require_malformed_unknown_safe() -> None:
    normalized = normalize_command_bundle({'steps': [{'op': 'where', 'unknown': object()}], 'warnings': 'not-a-list'})
    human = format_command_bundle_human(normalized)
    if '1. where: failed' not in human:
        raise SystemExit(f'malformed step did not normalize safely: {human!r}')
    if normalized['steps'][0].get('extras', {}).get('unknown') is None:
        raise SystemExit(f'non-json unknown field was not stringified/preserved: {normalized!r}')


def main() -> int:
    require_normalized_output()
    require_bundle_output_helpers()
    require_cli_json_mode()
    require_first_class_bundle_commands()
    require_generic_command_proof_output()
    require_malformed_unknown_safe()
    print('local output parser static smoke: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
