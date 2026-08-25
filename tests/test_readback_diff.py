from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_cli_v1.readback_diff import format_readback_diff_human, summarize_readback_diff


def _block(
    block_id: str,
    text_hash: str,
    *,
    inside_table: bool = False,
    font: str = 'Hamchorom Batang',
    size: float = 10.0,
    table: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        'block_id': block_id,
        'block_type': 'table_cell' if inside_table else 'paragraph',
        'inside_table': inside_table,
        'page_candidate': 1,
        'location': {'paragraph_index': 1, 'offset': 0},
        'table': table,
        'text': {'preview': block_id, 'normalized_hash': text_hash, 'text_len': 12},
        'style': {'font_family': font, 'font_size_pt': size, 'align': 'justify'},
        'warnings': [],
    }


def _manifest(*, font: str = 'Hamchorom Batang', size: float = 10.0, table_rows: int = 2, table_cols: int = 2, table_hash: str = 'sha256:cell-a') -> dict[str, object]:
    table_meta = {
        'target_id': 'tbl-1',
        'proof_hash': 'sha256:table-1',
        'cell_addr': 'A1',
        'row_1based': 1,
        'col_1based': 1,
        'row_count': table_rows,
        'col_count': table_cols,
        'row_span': 1,
        'col_span': 1,
    }
    return {
        'schema_version': 'local-output-parser/readback/v1',
        'ok': True,
        'read_only': True,
        'scope': 'document',
        'summary': 'readback document: fixture',
        'document': {'name': 'fixture.hwpx', 'page_count': 1},
        'page': {'current': 1, 'range': {'from': 1, 'to': 1}},
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
        'table_cells': [_block('cell-a', table_hash, inside_table=True, font=font, size=size, table=table_meta)],
        'controls': [
            {'target_id': 'tbl-1', 'type': 'tbl', 'page': 1, 'proof_hash': 'sha256:table-1'},
            {'target_id': 'pic-1', 'type': 'pic', 'page': 1, 'proof_hash': 'sha256:pic-1'},
        ],
        'caps': {'truncated': False},
        'warnings': [],
    }


class ReadbackDiffTests(unittest.TestCase):
    def test_font_family_drift_is_fail_priority(self) -> None:
        result = summarize_readback_diff(_manifest(), _manifest(font='Arial'))

        self.assertEqual(result['schema_version'], 'local-output-parser/readback-diff/v1')
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertIn('font_family_drift', result['risk_flags'])
        self.assertTrue(any(issue['category'] == 'font_family' for issue in result['issues']))
        human = format_readback_diff_human(result)
        self.assertIn('FAIL', human)
        self.assertIn('font family', human.lower())
        self.assertIn('read-only diagnostic', human.lower())

    def test_font_size_drift_and_body_size_inconsistency_are_fail_priority(self) -> None:
        candidate = _manifest(size=9.0)
        candidate['style_summary']['mixed_size_warning'] = True

        result = summarize_readback_diff(_manifest(), candidate)

        self.assertEqual(result['verdict'], 'FAIL')
        self.assertIn('font_size_drift', result['risk_flags'])
        self.assertIn('body_size_inconsistent', result['risk_flags'])
        self.assertTrue(any(issue['category'] == 'font_size' for issue in result['issues']))

    def test_native_table_structure_content_and_style_drift_writes_artifact_when_capped(self) -> None:
        candidate = _manifest(table_rows=3, table_cols=2, table_hash='sha256:cell-b')
        candidate['table_cells'][0]['style']['font_size_pt'] = 9.0
        with tempfile.TemporaryDirectory() as tmpdir:
            result = summarize_readback_diff(_manifest(), candidate, artifact_dir=Path(tmpdir), max_issues=1)
            artifact = Path(result['artifacts'][0]['path'])
            self.assertTrue(artifact.exists(), result['artifacts'])

        self.assertEqual(result['verdict'], 'FAIL')
        self.assertIn('native_table_drift', result['risk_flags'])
        self.assertIn('inside_table_style_drift', result['risk_flags'])
        self.assertTrue(result['caps']['truncated'])
        self.assertTrue(result['artifacts'], result)
        self.assertRegex(result['artifacts'][0]['sha256'], r'^sha256:[0-9a-f]{64}$')

    def test_stable_no_drift_output_is_pass_and_compact(self) -> None:
        result = summarize_readback_diff(_manifest(), _manifest())
        human = format_readback_diff_human(result)

        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(result['issues'], [])
        self.assertIn('PASS', human)
        self.assertLess(len(human), 1600)


if __name__ == '__main__':
    unittest.main()
