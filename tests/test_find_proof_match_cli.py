from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from app.local_cli_find_proof import resolve_find_proof_match_target
from local_cli_v1 import main as cli_main


class FindProofMatchPureTests(unittest.TestCase):
    def test_resolve_find_proof_match_uses_cached_text_and_duplicate_occurrence(self) -> None:
        matches = [
            {'number': 1, 'text': 'Repeated target paragraph', 'excerpt': 'Repeated target paragraph'},
            {'number': 2, 'text': 'Repeated target paragraph', 'excerpt': 'Repeated target paragraph'},
            {'number': 3, 'text': 'Different target paragraph', 'excerpt': 'Different target paragraph'},
        ]

        resolved = resolve_find_proof_match_target(matches, proof_match=2)

        self.assertEqual(resolved['number'], 2)
        self.assertEqual(resolved['query'], 'Repeated target paragraph')
        self.assertEqual(resolved['occurrence'], 2)
        self.assertEqual(resolved['match']['excerpt'], 'Repeated target paragraph')


class FindProofMatchCliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli_main.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_parser_accepts_find_proof_match_options(self) -> None:
        parser = cli_main.build_parser()

        args = parser.parse_args(
            [
                'find',
                '--proof-match',
                '2',
                '--proof-out-dir',
                '/tmp/proof-match',
                '--dpi',
                '180',
                '--contact-sheet',
                'needle',
            ]
        )

        self.assertEqual(args.command, 'find')
        self.assertEqual(args.text, 'needle')
        self.assertEqual(args.proof_match, 2)
        self.assertEqual(args.proof_out_dir, Path('/tmp/proof-match'))
        self.assertEqual(args.dpi, 180)
        self.assertTrue(args.contact_sheet)

    def test_default_find_proof_out_dir_is_source_based_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            first = tmp / 'source-find-match-002-page-007'
            first.mkdir()
            state = {'source_path': str(tmp / 'source.hwp'), 'source_filename': 'source.hwp'}

            destination = cli_main._find_proof_out_dir(state, match_number=2, page=7)

        self.assertEqual(destination, tmp / 'source-find-match-002-page-007-2')

    def test_find_proof_match_posts_page_request_and_renders_export_proof(self) -> None:
        original_post_json = cli_main.post_json
        original_execute_named_bundle = cli_main._execute_named_bundle
        original_render = cli_main._render_export_proof_manifest
        original_load_state = cli_main.load_state
        original_save_state = cli_main.save_state
        calls: dict[str, object] = {}
        state = {
            'base_url': 'http://fixture.local',
            'session_id': 'session-1',
            'source_path': '/tmp/source.hwp',
            'source_filename': 'source.hwp',
        }
        with tempfile.TemporaryDirectory() as tmp_raw:
            proof_dir = Path(tmp_raw) / 'proof'

            def fake_post_json(base_url: str, route: str, payload: dict):
                calls['post_json'] = (base_url, route, payload)
                self.assertEqual(route, '/local-cli/find')
                return {
                    'ok': True,
                    'query': payload['query'],
                    'match_count': 2,
                    'matches': [
                        {'number': 1, 'section': 'live', 'section_paragraph_index': 1, 'excerpt': 'first needle'},
                        {'number': 2, 'section': 'live', 'section_paragraph_index': 2, 'excerpt': 'second needle'},
                    ],
                    'proof_match': {
                        'number': 2,
                        'page': 7,
                        'page_evidence': {'page': 7, 'method': 'current_page'},
                    },
                }

            def fake_execute_named_bundle(base_url: str, bundle_name: str, bundle_args: list[str] | None = None):
                calls['bundle'] = (base_url, bundle_name, list(bundle_args or []))
                return object(), {'summary': 'export ok', 'steps': [{'op': 'export_pdf', 'result': {'download_path': '/download/source.pdf'}}]}

            def fake_render(**kwargs):
                calls['render'] = kwargs
                return {
                    'manifest_path': str(proof_dir / 'manifest.json'),
                    'exported_pdf_path': str(proof_dir / 'source.pdf'),
                    'pages_rendered': [7],
                    'pages_rendered_summary': '7',
                }

            def fake_load_state(path=None):  # noqa: ANN001
                return dict(state)

            def fake_save_state(new_state, path=None):  # noqa: ANN001
                state.clear()
                state.update(new_state)
                return Path('/tmp/state.json')

            try:
                cli_main.post_json = fake_post_json
                cli_main._execute_named_bundle = fake_execute_named_bundle
                cli_main._render_export_proof_manifest = fake_render
                cli_main.load_state = fake_load_state
                cli_main.save_state = fake_save_state

                rc, stdout, stderr = self.run_cli(
                    [
                        'find',
                        '--proof-match',
                        '2',
                        '--proof-out-dir',
                        str(proof_dir),
                        '--dpi',
                        '180',
                        '--contact-sheet',
                        'needle',
                    ]
                )
            finally:
                cli_main.post_json = original_post_json
                cli_main._execute_named_bundle = original_execute_named_bundle
                cli_main._render_export_proof_manifest = original_render
                cli_main.load_state = original_load_state
                cli_main.save_state = original_save_state

        self.assertEqual(rc, 0, stderr)
        self.assertIn('2. [live:2] second needle', stdout)
        self.assertIn('proof match: 2', stdout)
        self.assertIn('proof page: 7', stdout)
        self.assertIn('manifest:', stdout)
        self.assertIn('hwpx proof-packet --out-dir', stdout)
        self.assertEqual(calls['post_json'][2]['proof_match'], 2)  # type: ignore[index]
        self.assertEqual(calls['bundle'][1], 'export-proof-range')  # type: ignore[index]
        self.assertEqual(calls['bundle'][2], ['--pages', '7', '--dpi', '180', '--out-dir', str(proof_dir), '--contact-sheet', '--anchor', 'needle'])  # type: ignore[index]
        render_kwargs = calls['render']  # type: ignore[assignment]
        self.assertEqual(render_kwargs['pages'], [7])  # type: ignore[index]
        self.assertEqual(render_kwargs['dpi'], 180)  # type: ignore[index]
        self.assertEqual(render_kwargs['out_dir'], proof_dir)  # type: ignore[index]
        self.assertEqual(render_kwargs['anchors'], ['needle'])  # type: ignore[index]
        self.assertTrue(render_kwargs['contact_sheet_requested'])  # type: ignore[index]

    def test_find_proof_match_blocks_when_page_evidence_is_missing(self) -> None:
        original_post_json = cli_main.post_json
        original_execute_named_bundle = cli_main._execute_named_bundle
        original_load_state = cli_main.load_state
        original_save_state = cli_main.save_state
        state = {'base_url': 'http://fixture.local', 'session_id': 'session-1'}
        calls: list[str] = []

        def fake_post_json(base_url: str, route: str, payload: dict):
            return {
                'ok': True,
                'query': payload['query'],
                'matches': [{'number': 1, 'section': 'live', 'section_paragraph_index': 1, 'excerpt': 'needle'}],
                'proof_match': {'number': 1, 'page': None, 'page_evidence': {'page': None, 'method': None}},
            }

        def fake_execute_named_bundle(base_url: str, bundle_name: str, bundle_args: list[str] | None = None):
            calls.append(bundle_name)
            return object(), {}

        def fake_load_state(path=None):  # noqa: ANN001
            return dict(state)

        def fake_save_state(new_state, path=None):  # noqa: ANN001
            state.clear()
            state.update(new_state)
            return Path('/tmp/state.json')

        try:
            cli_main.post_json = fake_post_json
            cli_main._execute_named_bundle = fake_execute_named_bundle
            cli_main.load_state = fake_load_state
            cli_main.save_state = fake_save_state

            rc, _stdout, stderr = self.run_cli(['find', '--proof-match', '1', 'needle'])
        finally:
            cli_main.post_json = original_post_json
            cli_main._execute_named_bundle = original_execute_named_bundle
            cli_main.load_state = original_load_state
            cli_main.save_state = original_save_state

        self.assertEqual(rc, 1)
        self.assertIn('page evidence', stderr)
        self.assertEqual(calls, [])


    def test_last_proof_artifact_prefers_export_proof_page_over_pdf(self) -> None:
        state = {
            'last_export_path': '/tmp/source.pdf',
            'last_export_proof_page_paths': ['/tmp/page-007.png'],
        }

        self.assertEqual(cli_main._last_proof_artifact(state), ('export-proof rendered page', '/tmp/page-007.png'))


if __name__ == '__main__':
    unittest.main()
