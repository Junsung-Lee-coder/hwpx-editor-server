from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from local_cli_v1.main import build_command_status, build_parser, _record_export_proof_manifest_state
from local_cli_v1.proof_packet import ProofPacketError, build_proof_packet


class ProofPacketCliTests(unittest.TestCase):
    def test_parser_accepts_proof_packet_out_dir(self) -> None:
        parser = build_parser()

        args = parser.parse_args(['proof-packet', '--out-dir', '/tmp/proof'])

        self.assertEqual(args.command, 'proof-packet')
        self.assertEqual(args.out_dir, Path('/tmp/proof'))

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
            self.assertTrue(manifest['delivery_ready'])
            self.assertEqual(manifest['delivery_ready_reason'], 'packet includes a delivery source plus rendered proof artifact')
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
            self.assertTrue(manifest['delivery_ready'])
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

        self.assertEqual(updated['last_export_path'], '/tmp/source.pdf')
        self.assertEqual(updated['last_export_manifest_path'], '/tmp/proof/manifest.json')
        self.assertEqual(updated['last_export_proof_manifest_path'], '/tmp/proof/manifest.json')
        self.assertEqual(updated['last_export_proof_dir'], str(Path('/tmp/proof/manifest.json').expanduser().parent))
        self.assertEqual(updated['last_export_proof_contact_sheet_path'], '/tmp/proof/contact-sheet.png')
        self.assertEqual(updated['last_export_proof_page_paths'], ['/tmp/proof/page-001.png', '/tmp/proof/page-002.png'])

    def test_build_proof_packet_blocks_when_no_artifacts_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            with self.assertRaises(ProofPacketError) as ctx:
                build_proof_packet(out_dir=tmp / 'packet', state={'source_filename': 'source.hwp'})

        self.assertIn('No existing delivery artifacts', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
