from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_cli_v1.bundles import build_named_bundle


class Table4AnchorRangeReplaceBundleTests(unittest.TestCase):
    def test_builds_guarded_four_column_native_table_replacement_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            table_path = Path(tmp_dir) / 'table4.md'
            table_path.write_text(
                '| A | B | C | D |\n|---|---|---|---|\n| a | b | c | d |\n',
                encoding='utf-8',
            )
            spec = build_named_bundle(
                'table4-anchor-range-replace',
                [
                    '--from-file',
                    str(table_path),
                    '--section-anchor',
                    '3.2 Test conditions',
                    '--start-anchor',
                    'Table 4. Sample measurements summary',
                    '--end-before-anchor',
                    'Figure 5.',
                    '--required-source-basename',
                    'clean.hwp',
                    '--forbid-source-basename',
                    'discard.hwp',
                    '--caption-text',
                    'Table 4. Caption',
                    '--confirm-replace',
                ],
            )
            payload = spec.server_payload()
            self.assertEqual(spec.name, 'table4-anchor-range-replace')
            self.assertEqual(len(payload['steps']), 1)
            step = payload['steps'][0]
            self.assertEqual(step['op'], 'anchor_range_replace_native_table')
            self.assertEqual(step['rows'], 2)
            self.assertEqual(step['cols'], 4)
            self.assertIs(step['confirm_replace'], True)
            self.assertEqual(step['required_source_basename'], 'clean.hwp')
            self.assertEqual(step['forbid_source_basename'], 'discard.hwp')
            self.assertEqual(step['caption_text'], 'Table 4. Caption')
            self.assertEqual(step['end_before_anchor'], 'Figure 5.')


if __name__ == '__main__':
    unittest.main()
