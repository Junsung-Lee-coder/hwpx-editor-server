from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.local_cli_type_guard import type_insert_guard_reason
from local_cli_v1 import main as cli_main


class TypeGuardTests(unittest.TestCase):
    def test_blocks_insert_when_recent_safe_selection_was_lost(self) -> None:
        binding = {
            'last_selection': {
                'selected_text': 'old body',
                'selected_text_hash': 'sha256:abc',
                'safe_for_type': True,
                'active_selection_verified': True,
                'proof_source': 'hwpx select',
            }
        }

        reason = type_insert_guard_reason(
            binding,
            had_selection=False,
            restored_cached_selection=False,
            allow_insert_at_caret=False,
        )

        self.assertIsNotNone(reason)
        self.assertIn('recent safe selection', reason or '')
        self.assertIn('--insert-at-caret', reason or '')

    def test_allows_explicit_insert_at_caret_even_after_recent_selection(self) -> None:
        binding = {'last_selection': {'selected_text': 'old body', 'safe_for_type': True}}

        reason = type_insert_guard_reason(
            binding,
            had_selection=False,
            restored_cached_selection=False,
            allow_insert_at_caret=True,
        )

        self.assertIsNone(reason)

    def test_allows_replacement_when_live_or_restored_selection_exists(self) -> None:
        binding = {'last_selection': {'selected_text': 'old body', 'safe_for_type': True}}

        self.assertIsNone(
            type_insert_guard_reason(
                binding,
                had_selection=True,
                restored_cached_selection=False,
                allow_insert_at_caret=False,
            )
        )
        self.assertIsNone(
            type_insert_guard_reason(
                binding,
                had_selection=False,
                restored_cached_selection=True,
                allow_insert_at_caret=False,
            )
        )


class ArtifactDestinationTests(unittest.TestCase):
    def test_explicit_out_path_is_used_without_auto_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'deliverable.pdf'
            destination = cli_main._artifact_destination('export', out=out)

        self.assertEqual(destination, out)

    def test_explicit_out_dir_uses_default_filename_inside_directory(self) -> None:
        state = {
            'source_path': '/work/source/example.hwp',
            'source_filename': 'example.hwp',
        }
        with tempfile.TemporaryDirectory() as tmp:
            destination = cli_main._artifact_destination('page-screenshot', page=2, out_dir=Path(tmp), state=state)

        self.assertEqual(destination, Path(tmp) / 'example-page-002.png')

    def test_parser_accepts_safe_insert_and_artifact_output_options(self) -> None:
        parser = cli_main.build_parser()

        type_args = parser.parse_args(['type', '--insert-at-caret', 'hello'])
        save_args = parser.parse_args(['save', '--out', '/tmp/final.hwp'])
        page_args = parser.parse_args(['page-screenshot', '--page', '2', '--out-dir', '/tmp/proof'])
        export_args = parser.parse_args(['export', '--out', '/tmp/final.pdf'])

        self.assertTrue(type_args.insert_at_caret)
        self.assertEqual(save_args.out, Path('/tmp/final.hwp'))
        self.assertEqual(page_args.out_dir, Path('/tmp/proof'))
        self.assertEqual(export_args.out, Path('/tmp/final.pdf'))


if __name__ == '__main__':
    unittest.main()
