from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_cli_v1 import main as cli_main
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

    def test_runtime_candidate_identity_is_read_from_install_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = {
                'schema_version': 'hwpx/windows-install-marker/v1',
                'repository': 'github:example/project',
                'commit': 'c' * 40,
                'tree': 'd' * 40,
            }
            source_file = root / 'local_cli_v1' / 'main.py'
            source_file.parent.mkdir(parents=True)
            source_file.write_bytes(b'installed cli module')
            source_manifest = {
                'schema_version': 'hwpx/source-bundle/v1',
                'identity_source': 'asserted-gitless',
                'identity_verified': False,
                'repository': marker['repository'],
                'commit': marker['commit'],
                'tree': marker['tree'],
                'file_count': 1,
                'files': [{
                    'path': 'local_cli_v1/main.py',
                    'size': source_file.stat().st_size,
                    'sha256': hashlib.sha256(source_file.read_bytes()).hexdigest(),
                }],
                'archive_sha256': 'e' * 64,
            }
            manifest_path = root / 'source-manifest.json'
            manifest_path.write_text(json.dumps(source_manifest) + '\n', encoding='utf-8')
            marker['source_manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            (root / '.hwpx-install.json').write_text(json.dumps(marker), encoding='utf-8')

            identity = cli_main._read_candidate_identity(root)

        self.assertEqual(identity['repository'], marker['repository'])
        self.assertEqual(identity['commit'], marker['commit'])
        self.assertEqual(identity['tree'], marker['tree'])
        self.assertEqual(identity['manifest_sha256'], marker['source_manifest_sha256'])
        self.assertEqual(
            identity['candidate_generation'],
            ':'.join((marker['commit'], marker['tree'], marker['source_manifest_sha256'])),
        )

    def test_lifecycle_identity_projection_preserves_candidate_fields(self) -> None:
        identity = {
            'repository': 'github:example/project',
            'commit': 'c' * 40,
            'tree': 'd' * 40,
            'manifest_sha256': 'a' * 64,
            'candidate_generation': f"{'c' * 40}:{'d' * 40}:{'a' * 64}",
        }
        with patch.object(cli_main, '_read_candidate_identity', return_value=identity):
            with patch.object(cli_main, 'load_state', return_value={'session_id': 'session-1'}):
                payload = cli_main._with_lifecycle_identity({}, base_url='http://127.0.0.1:8765', command='where')

        self.assertEqual(payload['candidate_generation'], identity['candidate_generation'])
        self.assertEqual(payload['manifest_sha256'], identity['manifest_sha256'])
        self.assertEqual(payload['commit'], identity['commit'])

    def test_artifact_envelope_projects_candidate_identity(self) -> None:
        identity = {
            'repository': 'github:example/project',
            'commit': 'c' * 40,
            'tree': 'd' * 40,
            'manifest_sha256': 'a' * 64,
            'candidate_generation': f"{'c' * 40}:{'d' * 40}:{'a' * 64}",
        }
        with patch.object(cli_main, '_read_candidate_identity', return_value=identity):
            envelope = cli_main._artifact_envelope(
                role='rendered page proof',
                path=Path('page-001.png'),
                next_step='review',
                command='page-screenshot',
            )

        self.assertEqual(envelope['candidate_generation'], identity['candidate_generation'])
        self.assertEqual(envelope['manifest_sha256'], identity['manifest_sha256'])
        self.assertEqual(envelope['command'], 'page-screenshot')

    def test_status_projection_declares_its_top_level_schema(self) -> None:
        identity = {
            'repository': 'github:example/project',
            'commit': 'c' * 40,
            'tree': 'd' * 40,
            'manifest_sha256': 'a' * 64,
            'candidate_generation': f"{'c' * 40}:{'d' * 40}:{'a' * 64}",
        }
        with patch.object(cli_main, '_read_candidate_identity', return_value=identity):
            payload = cli_main._with_status_identity({'ok': True})

        self.assertEqual(payload['schema_version'], 'local-cli/status/v1')
        self.assertEqual(payload['command'], 'status')
        self.assertEqual(payload['candidate_generation'], identity['candidate_generation'])


if __name__ == '__main__':
    unittest.main()
