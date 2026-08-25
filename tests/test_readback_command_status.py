from __future__ import annotations

import unittest

from local_cli_v1.main import build_command_status, build_parser


class ReadbackCommandStatusTests(unittest.TestCase):
    def test_parser_exposes_readback_aliases_and_manifest(self) -> None:
        parser = build_parser()
        readback = parser.parse_args(['readback', '--scope', 'document'])
        read_context = parser.parse_args(['read-context', '--scope', 'selection'])
        read_manifest = parser.parse_args(['read-manifest'])
        readback_diff = parser.parse_args(['readback-diff', 'source.json', 'candidate.json', '--json'])

        self.assertEqual(readback.command, 'readback')
        self.assertEqual(readback.scope, 'document')
        self.assertEqual(read_context.command, 'read-context')
        self.assertEqual(read_context.scope, 'selection')
        self.assertEqual(read_manifest.command, 'read-manifest')
        self.assertEqual(read_manifest.scope, 'document')
        self.assertEqual(readback_diff.command, 'readback-diff')
        self.assertEqual(str(readback_diff.source_manifest), 'source.json')
        self.assertEqual(str(readback_diff.candidate_manifest), 'candidate.json')

    def test_command_status_marks_readback_commands_bundle_backed_and_bounded(self) -> None:
        status = build_command_status(build_parser())

        for command in ('readback', 'read-context', 'read-manifest'):
            with self.subTest(command=command):
                self.assertIn(command, status)
                self.assertEqual(status[command]['status'], 'bundle-backed')
                note = status[command]['note'].lower()
                self.assertIn('read-only', note)
                self.assertTrue(
                    'bounded' in note or 'compact' in note,
                    f'{command} note should document bounded/compact output: {note}',
                )

        self.assertIn('readback-diff', status)
        self.assertEqual(status['readback-diff']['status'], 'bundle-backed')
        diff_note = status['readback-diff']['note'].lower()
        self.assertIn('read-only', diff_note)
        self.assertIn('font', diff_note)
        self.assertIn('table', diff_note)


if __name__ == '__main__':
    unittest.main()
