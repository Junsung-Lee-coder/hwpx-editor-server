from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_cli_v1.main import build_command_status, build_parser, main as cli_main  # noqa: E402
from local_cli_v1.readback_diff import format_readback_diff_human, summarize_readback_diff  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _block(block_id: str, text_hash: str, *, inside_table: bool = False, font: str = 'Hamchorom Batang', size: float = 10.0, rows: int = 2) -> dict[str, object]:
    table = None
    if inside_table:
        table = {
            'target_id': 'tbl-1',
            'proof_hash': 'sha256:table-1',
            'cell_addr': 'A1',
            'row_1based': 1,
            'col_1based': 1,
            'row_count': rows,
            'col_count': 2,
            'row_span': 1,
            'col_span': 1,
        }
    return {
        'block_id': block_id,
        'block_type': 'table_cell' if inside_table else 'paragraph',
        'inside_table': inside_table,
        'page_candidate': 1,
        'table': table,
        'text': {'preview': block_id, 'normalized_hash': text_hash},
        'style': {'font_family': font, 'font_size_pt': size, 'align': 'justify'},
    }


def _manifest(*, font: str = 'Hamchorom Batang', size: float = 10.0, rows: int = 2, table_hash: str = 'sha256:cell-a') -> dict[str, object]:
    return {
        'schema_version': 'local-output-parser/readback/v1',
        'ok': True,
        'read_only': True,
        'scope': 'document',
        'summary': 'readback document: fixture',
        'document': {'name': 'fixture.hwpx', 'page_count': 1},
        'structure_summary': {
            'outside_text_block_count': 1,
            'table_count': 1,
            'table_cell_count': 1,
            'control_count': 2,
            'image_like_control_count': 1,
            'risk_flags': [],
        },
        'style_summary': {
            'font_family_counts': {font: 2},
            'font_size_pt_counts': {str(size): 2},
            'body_font_family_candidate': font,
            'body_font_size_pt_candidate': size,
            'mixed_font_warning': False,
            'mixed_size_warning': False,
        },
        'outside_text_blocks': [_block('body-1', 'sha256:body-a', font=font, size=size)],
        'table_cells': [_block('cell-a', table_hash, inside_table=True, font=font, size=size, rows=rows)],
        'controls': [
            {'target_id': 'tbl-1', 'type': 'tbl', 'page': 1, 'proof_hash': 'sha256:table-1'},
            {'target_id': 'pic-1', 'type': 'pic', 'page': 1, 'proof_hash': 'sha256:pic-1'},
        ],
        'caps': {'truncated': False},
        'warnings': [],
    }


def require_python_api() -> None:
    source = _manifest()
    candidate = _manifest(font='Arial', size=9.0, rows=3, table_hash='sha256:cell-b')
    result = summarize_readback_diff(source, candidate, max_issues=4)
    require(result.get('schema_version') == 'local-output-parser/readback-diff/v1', f'schema drifted: {result!r}')
    require(result.get('verdict') == 'FAIL', f'verdict should fail: {result!r}')
    for flag in ('font_family_drift', 'font_size_drift', 'native_table_drift'):
        require(flag in result.get('risk_flags', []), f'{flag} missing: {result!r}')
    human = format_readback_diff_human(result)
    for needle in ('FAIL', 'font', 'table', 'Read-only diagnostic'):
        require(needle in human, f'human summary missing {needle!r}:\n{human}')
    require(len(human) < 1800, f'human summary too long: {len(human)}')


def require_cli_and_status() -> None:
    parser = build_parser()
    parsed = parser.parse_args(['readback-diff', 'source.json', 'candidate.json', '--json'])
    require(parsed.command == 'readback-diff', f'parser lost command: {parsed!r}')
    status = build_command_status(parser)
    note = status.get('readback-diff', {}).get('note', '').lower()
    require('read-only' in note and 'font' in note and 'table' in note, f'command-status note too weak: {status.get("readback-diff")!r}')

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        source_path = tmp / 'source.json'
        candidate_path = tmp / 'candidate.json'
        artifact_dir = tmp / 'artifacts'
        source_path.write_text(json.dumps(_manifest(), ensure_ascii=False), encoding='utf-8')
        candidate_path.write_text(json.dumps(_manifest(font='Arial', size=9.0, rows=3, table_hash='sha256:cell-b'), ensure_ascii=False), encoding='utf-8')
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = cli_main(['readback-diff', str(source_path), str(candidate_path), '--json', '--artifact-dir', str(artifact_dir), '--max-issues', '1'])
        require(rc == 0, f'cli rc={rc}')
        payload = json.loads(stdout.getvalue())
        require(payload.get('verdict') == 'FAIL', f'cli payload should fail: {payload!r}')
        require(payload.get('caps', {}).get('truncated') is True, f'cli compact caps missing: {payload!r}')
        artifacts = payload.get('artifacts') or []
        require(artifacts and Path(artifacts[0]['path']).exists(), f'cli artifact missing: {payload!r}')
        require(str(artifacts[0].get('sha256', '')).startswith('sha256:'), f'cli artifact sha missing: {payload!r}')


def main() -> int:
    require_python_api()
    require_cli_and_status()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
