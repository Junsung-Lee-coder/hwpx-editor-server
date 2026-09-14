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

from app.local_cli_document import build_context, find_matches  # noqa: E402
from local_cli_v1.main import main as cli_main  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _fixture_paragraphs() -> list[dict[str, object]]:
    return [
        {
            'global_index': 1,
            'section': 'Contents/section0.xml',
            'section_paragraph_index': 1,
            'text': '1. Overview heading',
            'page_candidate': 1,
            'inside_table': False,
        },
        {
            'global_index': 2,
            'section': 'Contents/section0.xml',
            'section_paragraph_index': 2,
            'text': 'Outside target paragraph for the operator to inspect.',
            'page_candidate': 1,
            'inside_table': False,
        },
        {
            'global_index': 3,
            'section': 'Contents/section0.xml',
            'section_paragraph_index': 3,
            'text': '2. Evidence table',
            'page_candidate': 2,
            'inside_table': False,
        },
        {
            'global_index': 4,
            'section': 'Contents/section0.xml',
            'section_paragraph_index': 4,
            'text': 'Native table cell target text with exact evidence.',
            'page_candidate': 2,
            'inside_table': True,
            'table': {
                'target_id': 'ctrl/7/tbl/no-inst',
                'proof_hash': 'sha256:table-proof',
                'cell_addr': 'B2',
                'row_1based': 2,
                'col_1based': 2,
                'row_count': 3,
                'col_count': 2,
            },
        },
        {
            'global_index': 5,
            'section': 'Contents/section0.xml',
            'section_paragraph_index': 5,
            'text': 'After table paragraph.',
            'page_candidate': 2,
            'inside_table': False,
        },
        {
            'global_index': 6,
            'section': 'live-text',
            'section_paragraph_index': 6,
            'text': 'Plain live stream target without page evidence.',
            'inside_table': False,
        },
    ]


def _raw_find_payload() -> dict[str, object]:
    matches = find_matches(_fixture_paragraphs(), 'target', around=1, with_page=True)
    return {
        'schema_version': 'local-cli/find/v2',
        'ok': True,
        'read_only': True,
        'selection_mutated': False,
        'query': 'target',
        'around': 1,
        'with_page': True,
        'match_count': len(matches),
        'matches': matches,
        'proof_match': matches[0],
        'warnings': ['page candidates are approximate when sourced from live text'],
    }


def require_find_schema_and_context() -> None:
    matches = find_matches(_fixture_paragraphs(), 'target', around=1, with_page=True)
    require(len(matches) == 3, f'unexpected match count: {matches!r}')

    outside = matches[0]
    require(outside.get('number') == 1 and outside.get('match_index') == 1, f'match numbering missing: {outside!r}')
    require(str(outside.get('normalized_hash', '')).startswith('sha256:'), f'hash identity missing: {outside!r}')
    require(outside.get('read_only') is True, f'read-only marker missing: {outside!r}')
    require(outside.get('inside_table') is False, f'outside-table distinction missing: {outside!r}')
    require(outside.get('page_candidate') == 1, f'page candidate missing: {outside!r}')
    require(outside.get('location', {}).get('section') == 'Contents/section0.xml', f'location missing: {outside!r}')
    require(outside.get('context', {}).get('current') == outside.get('text'), f'current context missing: {outside!r}')
    require(outside.get('context', {}).get('before') == ['1. Overview heading'], f'before context missing: {outside!r}')
    require(outside.get('context', {}).get('after') == ['2. Evidence table'], f'after context missing: {outside!r}')
    require(outside.get('nearby_headings'), f'nearby headings missing: {outside!r}')

    table = matches[1]
    require(table.get('inside_table') is True, f'table distinction missing: {table!r}')
    require(table.get('table', {}).get('cell_addr') == 'B2', f'table cell identity missing: {table!r}')
    require(table.get('table', {}).get('target_id') == 'ctrl/7/tbl/no-inst', f'table target id missing: {table!r}')
    require(table.get('page_candidate') == 2, f'table page candidate missing: {table!r}')
    require('2. Evidence table' in table.get('nearby_headings', []), f'table nearby heading missing: {table!r}')

    approximate = matches[2]
    require(approximate.get('page_candidate') is None, f'missing page evidence should stay explicit: {approximate!r}')
    require(any('page' in warning.lower() and 'approximate' in warning.lower() for warning in approximate.get('warnings', [])), f'approximate page warning missing: {approximate!r}')

    context = build_context(_fixture_paragraphs(), table, radius=2)
    require(context.get('structured_context', {}).get('before') == ['Outside target paragraph for the operator to inspect.', '2. Evidence table'], f'structured before context missing: {context!r}')
    require(context.get('structured_context', {}).get('current') == table.get('text'), f'structured current context missing: {context!r}')
    require(context.get('structured_context', {}).get('after') == ['After table paragraph.', 'Plain live stream target without page evidence.'], f'structured after context missing: {context!r}')


def require_find_cli_json_and_options() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    captured: dict[str, object] = {}

    def fake_post_json(base_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        captured['base_url'] = base_url
        captured['path'] = path
        captured['payload'] = payload
        return _raw_find_payload()

    def fake_execute_named_bundle(base_url: str, bundle_name: str, bundle_args: list[str] | None = None):
        require(bundle_name == 'export-proof-range', f'unexpected bundle in find smoke: {bundle_name}')
        return object(), {'summary': 'export proof fixture', 'steps': [{'op': 'export_pdf', 'result': {'download_path': '/download/source.pdf'}}]}

    def fake_render_export_proof_manifest(**kwargs):
        return {
            'manifest_path': str(ROOT / 'tmp-find-proof-manifest.json'),
            'exported_pdf_path': str(ROOT / 'tmp-find-proof.pdf'),
            'pages_rendered': kwargs.get('pages') or [],
            'pages_rendered_summary': '1',
        }

    argv = [
        '--base-url',
        'http://unit.test',
        'find',
        'target',
        '--json',
        '--with-page',
        '--around',
        '1',
        '--proof-match',
        '1',
    ]
    with (
        patch('local_cli_v1.main.post_json', fake_post_json),
        patch('local_cli_v1.main._execute_named_bundle', fake_execute_named_bundle),
        patch('local_cli_v1.main._render_export_proof_manifest', fake_render_export_proof_manifest),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        rc = cli_main(argv)
    require(rc == 0, f'cli returned {rc}, stderr={stderr.getvalue()!r}')
    require(captured.get('path') == '/local-cli/find', f'find did not use find route: {captured!r}')
    request = captured.get('payload') if isinstance(captured.get('payload'), dict) else {}
    require(request.get('query') == 'target', f'query missing from find request: {request!r}')
    require(request.get('around') == 1, f'around option missing from find request: {request!r}')
    require(request.get('with_page') is True, f'with-page option missing from find request: {request!r}')
    require(request.get('proof_match') == 1, f'proof-match option missing from find request: {request!r}')

    parsed = json.loads(stdout.getvalue())
    require(parsed.get('schema_version') == 'local-cli/find/v2', f'find JSON schema missing: {parsed!r}')
    require(parsed.get('read_only') is True and parsed.get('selection_mutated') is False, f'read-only proof missing: {parsed!r}')
    require(parsed.get('proof_match', {}).get('match_index') == 1, f'proof-match payload missing: {parsed!r}')

    stdout = io.StringIO()
    with patch('local_cli_v1.main.post_json', fake_post_json), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli_main(['--base-url', 'http://unit.test', 'find', 'target', '--with-page', '--around', '1'])
    require(rc == 0, 'human find command failed')
    human = stdout.getvalue()
    for needle in ('page~1', 'outside-table', 'table B2', 'ctx before:'):
        require(needle in human, f'human find output missing {needle!r}:\n{human}')


def main() -> int:
    require_find_schema_and_context()
    require_find_cli_json_and_options()
    print('ok: find context static smoke')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
