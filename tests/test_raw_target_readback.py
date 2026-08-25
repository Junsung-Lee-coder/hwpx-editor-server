from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.raw_readback import RawReadbackMismatch, build_raw_target_readback


class RawTargetReadbackTests(unittest.TestCase):
    def test_writes_raw_after_text_artifact_with_hashes_lines_and_target_identity(self) -> None:
        text = '첫 줄\n둘째 줄\n셋째 줄'
        with tempfile.TemporaryDirectory() as tmpdir:
            readback = build_raw_target_readback(
                session_root=Path(tmpdir),
                operation_id='cell-replace:E5',
                raw_text=text,
                intended_text=text,
                target_identity={'kind': 'table-cell', 'cell_addr': 'E5', 'page': 3, 'pos': [0, 4, 0]},
                fallback_transform_log=[{'stage': 'insert', 'strategy': 'set_text_file', 'option': 'insertfile'}],
                fail_on_mismatch=True,
            )

            raw_path = Path(readback['raw_text_path'])
            manifest_path = Path(readback['manifest_path'])
            self.assertTrue(raw_path.exists(), readback)
            self.assertTrue(manifest_path.exists(), readback)
            self.assertEqual(raw_path.read_text(encoding='utf-8'), text)
            self.assertEqual(readback['raw_sha256'], 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest())
            self.assertEqual(readback['line_count'], 3)
            self.assertEqual(readback['target_identity']['cell_addr'], 'E5')
            self.assertEqual(readback['expected']['line_count'], 3)
            self.assertTrue(readback['checks']['normalized_hash_match'], readback)
            self.assertTrue(readback['checks']['line_sequence_match'], readback)
            self.assertEqual(readback['failures'], [])
            self.assertEqual(readback['fallback_transform_log'][0]['strategy'], 'set_text_file')

    def test_missing_or_changed_line_fails_closed(self) -> None:
        intended = '첫 줄\n둘째 줄\n셋째 줄'
        actual_missing_line = '첫 줄\n셋째 줄'
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RawReadbackMismatch, 'line_sequence_mismatch'):
                build_raw_target_readback(
                    session_root=Path(tmpdir),
                    operation_id='cell-replace:E5',
                    raw_text=actual_missing_line,
                    intended_text=intended,
                    target_identity={'kind': 'table-cell', 'cell_addr': 'E5', 'page': 3, 'pos': [0, 4, 0]},
                    fallback_transform_log=[{'stage': 'insert', 'strategy': 'set_text_file'}],
                    fail_on_mismatch=True,
                )


if __name__ == '__main__':
    unittest.main()
