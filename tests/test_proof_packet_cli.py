from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import local_cli_v1.main as cli_module
from local_cli_v1.main import build_command_status, build_parser, _record_export_proof_manifest_state
from local_cli_v1.proof_packet import ProofPacketError, build_proof_packet, seal_native_border_readback


class ProofPacketCliTests(unittest.TestCase):
    def test_parser_accepts_proof_packet_out_dir(self) -> None:
        parser = build_parser()

        args = parser.parse_args(['proof-packet', '--out-dir', '/tmp/proof'])

        self.assertEqual(args.command, 'proof-packet')
        self.assertEqual(args.out_dir, Path('/tmp/proof'))

    def test_parser_accepts_native_border_readback_inputs(self) -> None:
        parser = build_parser()

        args = parser.parse_args([
            'native-border-readback',
            '--pre-quit', '/tmp/pre.json',
            '--persisted', '/tmp/persisted.json',
            '--target-identity', '/tmp/target.json',
            '--out', '/tmp/native-border-readback.json',
        ])

        self.assertEqual(args.command, 'native-border-readback')
        self.assertEqual(args.pre_quit, Path('/tmp/pre.json'))
        self.assertEqual(args.persisted, Path('/tmp/persisted.json'))
        self.assertEqual(args.target_identity, Path('/tmp/target.json'))
        self.assertEqual(args.out, Path('/tmp/native-border-readback.json'))

    def test_candidate_identity_reader_rejects_unbound_marker_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            root = Path(tmp_raw)
            marker = root / '.hwpx-install.json'
            marker.write_text(
                json.dumps({
                    'repository': 'github:example/project',
                    'commit': 'not-a-commit',
                    'tree': 'tree',
                    'source_manifest_sha256': 'not-a-hash',
                }),
                encoding='utf-8',
            )

            self.assertEqual(cli_module._read_candidate_identity(root), {})

    def test_command_status_documents_proof_packet_as_local_only(self) -> None:
        status = build_command_status(build_parser())

        self.assertIn('proof-packet', status)
        self.assertEqual(status['proof-packet']['source'], 'parser-local')
        self.assertIn('no server call', status['proof-packet']['note'])
        self.assertIn('no document mutation', status['proof-packet']['note'])

    def test_build_proof_packet_copies_existing_artifacts_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            source_hwp = tmp / 'source.hwp'
            working_copy = tmp / 'edited.hwp'
            exported_pdf = tmp / 'result.pdf'
            rendered_png = tmp / 'page-002.png'
            export_manifest = tmp / 'export.manifest.json'
            source_hwp.write_bytes(b'original hwp metadata only')
            working_copy.write_bytes(b'edited hwp')
            exported_pdf.write_bytes(b'%PDF fake')
            rendered_png.write_bytes(b'png bytes')
            export_manifest.write_text('{"ok": true}\n', encoding='utf-8')
            state = {
                'source_path': str(source_hwp),
                'source_filename': 'source.hwp',
                'session_id': 's1',
                'last_saved_working_copy_path': str(working_copy),
                'last_export_path': str(exported_pdf),
                'last_page_screenshot_path': str(rendered_png),
                'last_export_manifest_path': str(export_manifest),
                'last_screenshot_path': str(tmp / 'missing-live.png'),
            }
            packet_dir = tmp / 'packet'

            manifest = build_proof_packet(out_dir=packet_dir, state=state, state_path=tmp / 'state.json')

            manifest_path = packet_dir / 'manifest.json'
            self.assertEqual(manifest['schema_version'], 'local-cli/proof-packet/v1')
            self.assertTrue(manifest['ok'])
            self.assertEqual(manifest['manifest_path'], str(manifest_path))
            self.assertEqual(manifest['packet_dir'], str(packet_dir))
            self.assertEqual(manifest['source_hwp_path'], str(source_hwp))
            self.assertEqual(manifest['source_filename'], 'source.hwp')
            self.assertEqual(manifest['session_id'], 's1')
            self.assertTrue(manifest['review_required'])
            self.assertFalse(manifest['delivery_ready'])
            self.assertIn('authenticated candidate-bound proof generation', manifest['delivery_ready_reason'])
            self.assertTrue(manifest_path.exists())

            by_role = {item['role']: item for item in manifest['artifacts']}
            self.assertEqual(set(by_role), {'saved_working_copy', 'exported_pdf', 'rendered_page_proof', 'export_manifest'})
            self.assertEqual((packet_dir / 'saved_working_copy.hwp').read_bytes(), b'edited hwp')
            self.assertEqual((packet_dir / 'exported_pdf.pdf').read_bytes(), b'%PDF fake')
            self.assertEqual((packet_dir / 'rendered_page_proof.png').read_bytes(), b'png bytes')
            self.assertEqual((packet_dir / 'export_manifest.json').read_text(encoding='utf-8'), '{"ok": true}\n')
            self.assertEqual(by_role['exported_pdf']['sha256'], hashlib.sha256(b'%PDF fake').hexdigest())
            self.assertEqual(by_role['exported_pdf']['bytes'], len(b'%PDF fake'))
            self.assertEqual(by_role['exported_pdf']['relative_path'], 'exported_pdf.pdf')
            self.assertEqual(by_role['exported_pdf']['state_key'], 'last_export_path')
            self.assertEqual(manifest['missing'][0]['state_key'], 'last_screenshot_path')
            self.assertIn('missing artifact', manifest['warnings'][0])

            written_manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            self.assertEqual(written_manifest['artifacts'], manifest['artifacts'])

    def test_build_proof_packet_copies_export_proof_range_pages_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            exported_pdf = tmp / 'source.pdf'
            export_manifest = tmp / 'manifest.json'
            page_one = tmp / 'page-001.png'
            page_two = tmp / 'page-002.png'
            contact_sheet = tmp / 'contact-sheet.png'
            exported_pdf.write_bytes(b'pdf')
            export_manifest.write_text('{"schema_version": "local-cli/export-proof-range/v1"}\n', encoding='utf-8')
            page_one.write_bytes(b'page 1')
            page_two.write_bytes(b'page 2')
            contact_sheet.write_bytes(b'sheet')
            state = {
                'source_filename': 'source.hwp',
                'last_export_path': str(exported_pdf),
                'last_export_manifest_path': str(export_manifest),
                'last_export_proof_page_paths': [str(page_one), str(page_two)],
                'last_export_proof_contact_sheet_path': str(contact_sheet),
            }

            manifest = build_proof_packet(out_dir=tmp / 'packet', state=state)

            roles = [item['role'] for item in manifest['artifacts']]
            self.assertEqual(roles.count('export_proof_page'), 2)
            self.assertIn('export_proof_contact_sheet', roles)
            self.assertFalse(manifest['delivery_ready'])
            self.assertIn('authenticated candidate-bound proof generation', manifest['delivery_ready_reason'])
            packet_files = {Path(item['packet_path']).name for item in manifest['artifacts']}
            self.assertIn('export_proof_page-001.png', packet_files)
            self.assertIn('export_proof_page-002.png', packet_files)
            self.assertIn('export_proof_contact_sheet.png', packet_files)

    def test_build_proof_packet_marks_save_only_packet_not_delivery_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            working_copy = tmp / 'edited.hwp'
            working_copy.write_bytes(b'edited only')

            manifest = build_proof_packet(
                out_dir=tmp / 'packet',
                state={'source_filename': 'source.hwp', 'last_saved_working_copy_path': str(working_copy)},
            )

            self.assertFalse(manifest['delivery_ready'])
            self.assertEqual(manifest['delivery_ready_reason'], 'packet is missing rendered proof artifact')
            self.assertIn('not delivery-ready', manifest['warnings'][-1])

    def test_record_export_proof_manifest_state_exposes_pages_for_proof_packet(self) -> None:
        state = {'source_filename': 'source.hwp'}
        manifest = {
            'exported_pdf_path': '/tmp/source.pdf',
            'manifest_path': '/tmp/proof/manifest.json',
            'contact_sheet_path': '/tmp/proof/contact-sheet.png',
            'pages': [
                {'page': 1, 'png_path': '/tmp/proof/page-001.png'},
                {'page': 2, 'png_path': '/tmp/proof/page-002.png'},
            ],
        }

        updated = _record_export_proof_manifest_state(state, manifest)
        expected_manifest_path = str(Path('/tmp/proof/manifest.json').expanduser())

        self.assertEqual(updated['last_export_path'], '/tmp/source.pdf')
        self.assertEqual(updated['last_export_manifest_path'], expected_manifest_path)
        self.assertEqual(updated['last_export_proof_manifest_path'], expected_manifest_path)
        self.assertEqual(updated['last_export_proof_dir'], str(Path(expected_manifest_path).parent))
        self.assertEqual(updated['last_export_proof_contact_sheet_path'], '/tmp/proof/contact-sheet.png')
        self.assertEqual(updated['last_export_proof_page_paths'], ['/tmp/proof/page-001.png', '/tmp/proof/page-002.png'])

    def test_build_proof_packet_blocks_when_no_artifacts_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            with self.assertRaises(ProofPacketError) as ctx:
                build_proof_packet(out_dir=tmp / 'packet', state={'source_filename': 'source.hwp'})

        self.assertIn('No existing delivery artifacts', str(ctx.exception))

    def test_build_proof_packet_hash_binds_persisted_native_border_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            working_copy = tmp / 'edited.hwp'
            rendered_png = tmp / 'page-001.png'
            native_readback = tmp / 'native-border-readback.json'
            working_copy.write_bytes(b'edited')
            rendered_png.write_bytes(b'png')
            native_readback.write_text(
                json.dumps(
                    {
                        'schema_version': 'local-cli/native-border-readback/v1',
                        'candidate_generation': 'commit-1:tree-1:sha-1',
                        'source_manifest_sha256': 'sha-1',
                        'target_identity': {'target_id': 'table-1-cell-A1'},
                        'pre_quit_readback': {'BorderTypeLeft': 0, 'DiagonalType': 0},
                        'persisted_readback': {'BorderTypeLeft': 0, 'DiagonalType': 0},
                    },
                    indent=2,
                )
                + '\n',
                encoding='utf-8',
            )
            state = {
                'source_filename': 'source.hwp',
                'session_id': 's1',
                'candidate_generation': 'commit-1:tree-1:sha-1',
                'source_manifest_sha256': 'sha-1',
                'target_identity': {'target_id': 'table-1-cell-A1'},
                'last_saved_working_copy_path': str(working_copy),
                'last_page_screenshot_path': str(rendered_png),
                'last_native_border_readback_path': str(native_readback),
            }

            manifest = build_proof_packet(out_dir=tmp / 'packet', state=state)

            native = [item for item in manifest['artifacts'] if item['role'] == 'native_border_readback']
            self.assertEqual(len(native), 1)
            self.assertEqual(native[0]['proof_binding']['candidate_generation'], 'commit-1:tree-1:sha-1')
            self.assertEqual(native[0]['proof_binding']['source_manifest_sha256'], 'sha-1')
            self.assertEqual(native[0]['proof_binding']['target_identity'], {'target_id': 'table-1-cell-A1'})
            self.assertEqual(native[0]['bytes'], native_readback.stat().st_size)
            self.assertEqual(manifest['proof_binding']['candidate_generation'], 'commit-1:tree-1:sha-1')

    def test_build_proof_packet_excludes_native_border_readback_from_other_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            working_copy = tmp / 'edited.hwp'
            rendered_png = tmp / 'page-001.png'
            native_readback = tmp / 'native-border-readback.json'
            working_copy.write_bytes(b'edited')
            rendered_png.write_bytes(b'png')
            native_readback.write_text(
                json.dumps(
                    {
                        'schema_version': 'local-cli/native-border-readback/v1',
                        'candidate_generation': 'old:tree:sha',
                        'source_manifest_sha256': 'old-sha',
                        'target_identity': {'target_id': 'table-1-cell-A1'},
                        'pre_quit_readback': {'BorderTypeLeft': 0},
                        'persisted_readback': {'BorderTypeLeft': 0},
                    }
                ),
                encoding='utf-8',
            )

            manifest = build_proof_packet(
                out_dir=tmp / 'packet',
                state={
                    'source_filename': 'source.hwp',
                    'candidate_generation': 'new:tree:sha',
                    'source_manifest_sha256': 'new-sha',
                    'target_identity': {'target_id': 'table-1-cell-A1'},
                    'last_saved_working_copy_path': str(working_copy),
                    'last_page_screenshot_path': str(rendered_png),
                    'last_native_border_readback_path': str(native_readback),
                },
            )

            self.assertNotIn('native_border_readback', {item['role'] for item in manifest['artifacts']})
            self.assertTrue(any('native border' in warning.lower() for warning in manifest['warnings']))

    def test_native_border_readback_sealer_writes_candidate_bound_value_level_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            destination = Path(tmp_raw) / 'native-border-readback.json'

            result = seal_native_border_readback(
                destination=destination,
                candidate_generation='commit-1:tree-1:sha-1',
                source_manifest_sha256='sha-1',
                target_identity={'target_id': 'table-1-cell-A1'},
                pre_quit_readback={'BorderTypeLeft': 0, 'DiagonalType': 0},
                persisted_readback={'BorderTypeLeft': 0, 'DiagonalType': 0},
            )

            self.assertEqual(result['schema_version'], 'local-cli/native-border-readback/v1')
            self.assertEqual(result['candidate_generation'], 'commit-1:tree-1:sha-1')
            self.assertEqual(result['source_manifest_sha256'], 'sha-1')
            self.assertEqual(result['target_identity'], {'target_id': 'table-1-cell-A1'})
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8')), result)

    def test_native_border_readback_cli_records_artifact_and_state_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            pre_quit = tmp / 'pre.json'
            persisted = tmp / 'persisted.json'
            target = tmp / 'target.json'
            destination = tmp / 'native-border-readback.json'
            pre_quit.write_text(json.dumps({'BorderTypeLeft': 0, 'DiagonalType': 0}), encoding='utf-8')
            persisted.write_text(json.dumps({'BorderTypeLeft': 0, 'DiagonalType': 0}), encoding='utf-8')
            target.write_text(json.dumps({'target_id': 'table-1-cell-A1'}), encoding='utf-8')
            state = {'session_id': 's1', 'source_filename': 'source.hwp'}
            saved_states: list[dict[str, object]] = []

            with (
                mock.patch.object(cli_module, '_resolve_base_url', return_value='http://127.0.0.1:8765'),
                mock.patch.object(cli_module, '_read_candidate_identity', return_value={
                    'repository': 'github:example/project',
                    'commit': 'a' * 40,
                    'tree': 'b' * 40,
                    'manifest_sha256': 'c' * 64,
                    'candidate_generation': f"{'a' * 40}:{'b' * 40}:{'c' * 64}",
                }),
                mock.patch.object(cli_module, 'load_state', return_value=state),
                mock.patch.object(
                    cli_module,
                    'update_state',
                    side_effect=lambda updater, **_kwargs: saved_states.append(dict(updater(dict(state)))) or dict(saved_states[-1]),
                ),
            ):
                exit_code = cli_module.main([
                    'native-border-readback',
                    '--pre-quit', str(pre_quit),
                    '--persisted', str(persisted),
                    '--target-identity', str(target),
                    '--out', str(destination),
                    '--json',
                ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(saved_states), 1)
            self.assertEqual(saved_states[0]['last_native_border_readback_path'], str(destination.resolve()))
            self.assertEqual(saved_states[0]['target_identity'], {'target_id': 'table-1-cell-A1'})
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8'))['persisted_readback']['BorderTypeLeft'], 0)

    def test_native_border_readback_sealer_rejects_missing_persisted_value_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            with self.assertRaisesRegex(ProofPacketError, 'persisted_readback'):
                seal_native_border_readback(
                    destination=Path(tmp_raw) / 'native-border-readback.json',
                    candidate_generation='commit-1:tree-1:sha-1',
                    source_manifest_sha256='sha-1',
                    target_identity={'target_id': 'table-1-cell-A1'},
                    pre_quit_readback={'BorderTypeLeft': 0},
                    persisted_readback={},
                )

    def test_build_proof_packet_rejects_empty_pre_quit_native_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            working_copy = tmp / 'edited.hwp'
            rendered_png = tmp / 'page-001.png'
            native_readback = tmp / 'native-border-readback.json'
            working_copy.write_bytes(b'edited')
            rendered_png.write_bytes(b'png')
            native_readback.write_text(
                json.dumps(
                    {
                        'schema_version': 'local-cli/native-border-readback/v1',
                        'candidate_generation': 'commit-1:tree-1:sha-1',
                        'source_manifest_sha256': 'sha-1',
                        'target_identity': {'target_id': 'table-1-cell-A1'},
                        'pre_quit_readback': {},
                        'persisted_readback': {'BorderTypeLeft': 0},
                    }
                ),
                encoding='utf-8',
            )

            manifest = build_proof_packet(
                out_dir=tmp / 'packet',
                state={
                    'source_filename': 'source.hwp',
                    'candidate_generation': 'commit-1:tree-1:sha-1',
                    'source_manifest_sha256': 'sha-1',
                    'target_identity': {'target_id': 'table-1-cell-A1'},
                    'last_saved_working_copy_path': str(working_copy),
                    'last_page_screenshot_path': str(rendered_png),
                    'last_native_border_readback_path': str(native_readback),
                },
            )

            self.assertNotIn('native_border_readback', {item['role'] for item in manifest['artifacts']})
            self.assertTrue(any('native border' in warning.lower() for warning in manifest['warnings']))


if __name__ == '__main__':
    unittest.main()
