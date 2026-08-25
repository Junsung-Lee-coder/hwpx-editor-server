from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_cli_v1.bundles import build_named_bundle  # noqa: E402
from local_cli_v1.main import build_command_status, build_parser, main as cli_main  # noqa: E402
from local_cli_v1.output_parser import format_readback_human, summarize_readback  # noqa: E402
from app.command_packages.commands.readback import run as readback_run  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _readback_raw(scope: str = 'document') -> dict[str, object]:
    return {
        'ok': True,
        'command': 'command-bundle',
        'summary': f'readback {scope}: read-only ok',
        'steps': [
            {
                'index': 1,
                'label': f'readback:{scope}',
                'op': 'readback',
                'ok': True,
                'dirty': False,
                'result': {
                    'schema_version': 'local-cli/readback/v1',
                    'ok': True,
                    'read_only': True,
                    'scope': scope,
                    'summary': f'readback {scope}: pages=2 blocks=2 tables=1 controls=2',
                    'document': {
                        'name': 'fixture.hwpx',
                        'path': '/tmp/fixture.hwpx',
                        'working_copy_id': 'fixture-session',
                        'page_count': 2,
                        'is_modified': False,
                    },
                    'page': {
                        'current': 1,
                        'range': {'from': 1, 'to': 2, 'pages': [1, 2]},
                        'method': 'fixture-page-evidence',
                        'warnings': ['page evidence approximate'],
                    },
                    'structure_summary': {
                        'outside_text_block_count': 2,
                        'table_count': 1,
                        'table_cell_count': 1,
                        'control_count': 2,
                        'image_like_control_count': 1,
                        'page_break_or_blank_risks': [],
                        'risk_flags': ['table_text_separated'],
                    },
                    'style_summary': {
                        'font_family_counts': {'Hamchorom Batang': 2},
                        'font_size_pt_counts': {'10.0': 2},
                        'body_font_family_candidate': 'Hamchorom Batang',
                        'body_font_size_pt_candidate': 10.0,
                        'alignment_counts': {'justify': 1},
                        'line_spacing_counts': {'160': 1},
                        'mixed_font_warning': False,
                        'mixed_size_warning': False,
                    },
                    'current_block': {
                        'block_id': 'body/0/12',
                        'block_type': 'paragraph',
                        'inside_table': False,
                        'page_candidate': 1,
                        'location': {'list_id': 0, 'paragraph_index': 12, 'offset': 5},
                        'table': None,
                        'text': {
                            'preview': 'Outside current paragraph',
                            'text_len': 25,
                            'normalized_hash': 'sha256:outside-current',
                            'line_break_count': 0,
                            'wrap_evidence': {'available': False},
                        },
                        'style': {
                            'font_family': 'Hamchorom Batang',
                            'font_size_pt': 10.0,
                            'bold': False,
                            'align': 'justify',
                            'line_spacing': 160,
                        },
                        'context': {
                            'previous': 'Before outside paragraph',
                            'current': 'Outside current paragraph',
                            'next': 'After outside paragraph',
                            'heading_path': [],
                        },
                        'warnings': [],
                    },
                    'outside_text_blocks': [
                        {
                            'block_id': 'body/0/12',
                            'block_type': 'paragraph',
                            'inside_table': False,
                            'page_candidate': 1,
                            'text': {'preview': 'Outside current paragraph', 'normalized_hash': 'sha256:outside-current'},
                            'style': {'font_family': 'Hamchorom Batang', 'font_size_pt': 10.0, 'align': 'justify'},
                            'warnings': [],
                        },
                        {
                            'block_id': 'body/0/13',
                            'block_type': 'paragraph',
                            'inside_table': False,
                            'page_candidate': 1,
                            'text': {'preview': 'Second outside paragraph', 'normalized_hash': 'sha256:outside-second'},
                            'style': {'font_family': 'Hamchorom Batang', 'font_size_pt': 10.0, 'align': 'justify'},
                            'warnings': [],
                        },
                    ],
                    'table_cells': [
                        {
                            'block_id': 'table/ctrl/1/tbl/no-inst/r1c2',
                            'block_type': 'table_cell',
                            'inside_table': True,
                            'page_candidate': 1,
                            'table': {
                                'target_id': 'ctrl/1/tbl/no-inst',
                                'proof_hash': 'sha256:table-proof',
                                'cell_addr': 'B1',
                                'row_1based': 1,
                                'col_1based': 2,
                                'row_count': 3,
                                'col_count': 2,
                            },
                            'text': {'preview': 'Native table cell text', 'normalized_hash': 'sha256:table-cell'},
                            'style': {'font_family': 'Hamchorom Batang', 'font_size_pt': 9.0, 'align': 'center'},
                            'warnings': [],
                        }
                    ],
                    'controls': [
                        {'target_id': 'ctrl/0/pic/no-inst', 'type': 'pic', 'page': 1, 'proof_hash': 'sha256:pic', 'risk_flags': []},
                        {'target_id': 'ctrl/1/tbl/no-inst', 'type': 'tbl', 'page': 1, 'proof_hash': 'sha256:table-proof', 'risk_flags': []},
                    ],
                    'caps': {
                        'max_blocks': 1,
                        'max_table_cells': 1,
                        'max_controls': 1,
                        'truncated': False,
                        'raw_artifact_path': '/tmp/read-manifest.json',
                    },
                    'warnings': ['line spacing unavailable from fixture'],
                },
            }
        ],
    }


def require_readback_schema_and_caps() -> None:
    payload = summarize_readback(_readback_raw('document'))
    require(payload.get('schema_version') == 'local-output-parser/readback/v1', f'unexpected schema: {payload!r}')
    require(payload.get('ok') is True and payload.get('read_only') is True, f'read-only fields missing: {payload!r}')
    require(payload.get('scope') == 'document', f'scope lost: {payload!r}')
    require(payload.get('structure_summary', {}).get('table_count') == 1, f'table count lost: {payload!r}')
    require(payload.get('style_summary', {}).get('font_family_counts'), f'font family counts missing: {payload!r}')
    require(payload.get('current_block', {}).get('inside_table') is False, f'current block table flag lost: {payload!r}')

    outside = payload.get('outside_text_blocks') or []
    table_cells = payload.get('table_cells') or []
    controls = payload.get('controls') or []
    require(len(outside) == 1, f'outside block cap not enforced: {outside!r}')
    require(len(table_cells) == 1, f'table cell cap drifted: {table_cells!r}')
    require(len(controls) == 1, f'control cap not enforced: {controls!r}')
    require(payload.get('caps', {}).get('truncated') is True, f'truncation flag missing: {payload.get("caps")!r}')
    require(payload.get('caps', {}).get('raw_artifact_path') == '/tmp/read-manifest.json', f'artifact path lost: {payload.get("caps")!r}')

    outside_hashes = {item.get('text', {}).get('normalized_hash') for item in outside if isinstance(item, dict)}
    table_hashes = {item.get('text', {}).get('normalized_hash') for item in table_cells if isinstance(item, dict)}
    require('sha256:table-cell' in table_hashes, f'table-cell hash missing from table_cells: {table_cells!r}')
    require(not outside_hashes.intersection(table_hashes), f'table text leaked into outside blocks: outside={outside!r} table={table_cells!r}')
    require(any('line spacing unavailable' in warning for warning in payload.get('warnings', [])), f'unavailable warning missing: {payload!r}')

    human = format_readback_human(_readback_raw('document'))
    for needle in ('readback document:', 'structure:', 'body style:', 'native tables:', 'warnings:'):
        require(needle in human, f'human readback missing {needle!r}:\n{human}')
    require(len(human) < 2500, f'human readback is not compact: {len(human)} chars')


def require_caret_and_selection_scopes() -> None:
    caret = summarize_readback(_readback_raw('caret'))
    require(caret.get('scope') == 'caret', f'caret scope lost: {caret!r}')
    require(caret.get('current_block', {}).get('block_type') == 'paragraph', f'caret current block missing: {caret!r}')
    selection = summarize_readback(_readback_raw('selection'))
    require(selection.get('scope') == 'selection', f'selection scope lost: {selection!r}')
    require('selection' in format_readback_human(_readback_raw('selection')).splitlines()[0], 'selection human summary missing scope')


def require_actual_readback_package_marks_broad_scope_partial() -> None:
    context_payload = {
        'ok': True,
        'document': {'name': 'fixture.hwpx', 'path': '/tmp/fixture.hwpx', 'is_modified': False},
        'page': {'current': 2, 'page_count': 5, 'method': 'fixture-page-evidence'},
        'block_context': {'inside_table': False, 'list_id': 0, 'paragraph_index': 41, 'offset': 3},
        'paragraph_context': {
            'paragraph_number_1based': 42,
            'current_paragraph_text': 'Current caret paragraph only',
            'current_paragraph_preview': 'Current caret paragraph only',
            'previous_paragraph_preview': 'Previous paragraph',
            'next_paragraph_preview': 'Next paragraph',
        },
        'line_context': {'method': 'fixture', 'approximation': False, 'current_visual_line_preview': 'Current line'},
        'style_summary': {
            'character': {'face_name': 'Hamchorom Batang', 'font_size_pt': 10.0, 'bold': False},
            'paragraph': {'align': 'justify', 'line_spacing': 160},
        },
        'nearby_text': {'current': 'Current caret paragraph only'},
        'structure_signals': {'has_selection': False, 'selection_mode': 0},
        'warnings': [],
    }

    class FakeService:
        def _bundle_control_inventory(self, hwp: object, step: dict[str, object]) -> dict[str, object]:
            return {
                'items': [{'target_id': 'ctrl/1/tbl/no-inst', 'type': 'tbl', 'page': 2, 'proof_hash': 'sha256:table'}],
                'control_count_total': 1,
                'control_count_returned': 1,
                'warnings': [],
            }

    def fake_context_step(**kwargs: object) -> tuple[dict[str, object], bool, list[str]]:
        return context_payload, False, []

    with tempfile.TemporaryDirectory() as tmpdir:
        handle = SimpleNamespace(hwp=object(), session_root=Path(tmpdir))
        with patch('app.command_packages.commands.readback.run.run_context_step', fake_context_step), patch(
            'app.command_packages.commands.readback.run._get_document_text',
            lambda hwp: 'Current caret paragraph only\nSecond document paragraph\nTable flattened text',
        ):
            result, dirty, warnings = readback_run.run_step(
                service=FakeService(),
                handle=handle,
                step={'scope': 'document', 'max_blocks': 5, 'max_table_cells': 5, 'max_controls': 5},
                binding=None,
                manifest={'version': 'local-cli/readback/v1-package'},
            )

    require(dirty is False, f'readback package dirtied document: {dirty!r}')
    require(result.get('scope') == 'document' and result.get('read_only') is True, f'package result drifted: {result!r}')
    structure = result.get('structure_summary', {}) if isinstance(result.get('structure_summary'), dict) else {}
    require(structure.get('broad_text_block_enumeration_available') is False, f'broad enumeration marker missing: {structure!r}')
    require(structure.get('outside_text_blocks_scope') == 'current_block_only', f'outside scope marker missing: {structure!r}')
    require(structure.get('table_cells_scope') == 'current_block_only', f'table-cell scope marker missing: {structure!r}')
    require(any('current-block-only' in warning for warning in [*warnings, *result.get('warnings', [])]), f'partial warning missing: {result!r}')

    bundle_raw = {'ok': True, 'command': 'command-bundle', 'steps': [{'index': 1, 'op': 'readback', 'ok': True, 'dirty': False, 'result': result}]}
    normalized = summarize_readback(bundle_raw)
    document_text_summary = normalized.get('document_text_summary', {}) if isinstance(normalized.get('document_text_summary'), dict) else {}
    require(document_text_summary.get('available') is True, f'document_text_summary dropped: {normalized!r}')
    require(document_text_summary.get('nonempty_line_count') == 3, f'document_text_summary counts lost: {document_text_summary!r}')
    require(normalized.get('caps', {}).get('raw_artifact_path'), f'raw artifact path missing: {normalized.get("caps")!r}')
    require(normalized.get('caps', {}).get('raw_artifact_sha256'), f'raw artifact sha missing: {normalized.get("caps")!r}')
    human = format_readback_human(bundle_raw)
    for needle in ('coverage: broad text block/table-cell arrays are current-block-only', 'document text: chars=', 'lines=3'):
        require(needle in human, f'human readback missing {needle!r}:\n{human}')


def require_command_layer() -> None:
    parser = build_parser()
    parser_names = {name for action in parser._actions if isinstance(getattr(action, 'choices', None), dict) for name in action.choices}
    require({'readback', 'read-manifest'} <= parser_names, f'readback commands missing from parser: {sorted(parser_names)}')
    status = build_command_status(parser)
    require(status.get('readback', {}).get('status') == 'bundle-backed', f'readback command-status missing: {status.get("readback")!r}')
    require('LLM-friendly' in status.get('readback', {}).get('note', ''), f'readback command-status note too vague: {status.get("readback")!r}')
    require(status.get('read-manifest', {}).get('status') == 'bundle-backed', f'read-manifest status missing: {status.get("read-manifest")!r}')

    spec = build_named_bundle('readback', ['--scope', 'document', '--max-blocks', '1', '--max-controls', '1'])
    steps = spec.server_payload().get('steps')
    require(isinstance(steps, list) and steps and steps[0].get('op') == 'readback', f'readback bundle payload drifted: {steps!r}')
    require(steps[0].get('scope') == 'document' and steps[0].get('max_blocks') == 1, f'readback bundle args lost: {steps!r}')

    stdout = io.StringIO()
    stderr = io.StringIO()
    captured: dict[str, object] = {}

    def fake_post_json(base_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        captured['base_url'] = base_url
        captured['path'] = path
        captured['payload'] = payload
        return _readback_raw('document')

    with patch('local_cli_v1.main.post_json', fake_post_json), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli_main(['--base-url', 'http://unit.test', 'readback', '--scope', 'document', '--json', '--max-blocks', '1', '--max-controls', '1'])
    require(rc == 0, f'cli returned {rc}, stderr={stderr.getvalue()!r}')
    require(captured.get('path') == '/local-cli/command-bundle', f'cli did not use command-bundle: {captured!r}')
    request_steps = captured.get('payload', {}).get('steps') if isinstance(captured.get('payload'), dict) else None
    require(isinstance(request_steps, list) and request_steps[0].get('op') == 'readback', f'cli readback request drifted: {captured!r}')
    parsed = json.loads(stdout.getvalue())
    require(parsed.get('schema_version') == 'local-output-parser/readback/v1', f'cli JSON not normalized readback: {parsed!r}')


def main() -> int:
    require_readback_schema_and_caps()
    require_caret_and_selection_scopes()
    require_actual_readback_package_marks_broad_scope_partial()
    require_command_layer()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
