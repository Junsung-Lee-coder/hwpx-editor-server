from __future__ import annotations

import unittest

from app.local_cli_status import build_local_cli_status_payload


class LocalCliStatusPayloadTests(unittest.TestCase):
    def test_ready_runtime_without_live_session_opens_next(self) -> None:
        payload = build_local_cli_status_payload(
            snapshot={
                'ready': True,
                'checks': {'hancom_automation': {'ok': True}},
                'errors': [],
            },
            active_binding=None,
            live_bound=False,
        )

        self.assertTrue(payload['ok'])
        self.assertTrue(payload['runtime_up'])
        self.assertTrue(payload['hancom_attached'])
        self.assertIsNone(payload['blocked_reason'])
        self.assertEqual(payload['next_action'], 'open a file')
        self.assertIsNone(payload['session_id'])
        self.assertFalse(payload['live_session_bound'])

    def test_ready_runtime_with_live_session_keeps_artifact_visibility(self) -> None:
        payload = build_local_cli_status_payload(
            snapshot={
                'ready': True,
                'checks': {'hancom_automation': {'ok': True}},
                'errors': [],
            },
            active_binding={
                'session_id': 's1',
                'source_filename': 'source.hwpx',
                'working_copy_path': 'C:/work/source.hwpx',
                'working_copy_dirty': True,
                'artifacts': {
                    'latest_export_path': 'C:/work/export.pdf',
                    'latest_screenshot_path': 'C:/work/page.png',
                },
            },
            live_bound=True,
        )

        self.assertEqual(payload['next_action'], 'continue with find/where/select or capture rendered proof before saving/reporting')
        self.assertEqual(payload['session_id'], 's1')
        self.assertEqual(payload['active_document'], 'source.hwpx')
        self.assertEqual(payload['working_copy_path'], 'C:/work/source.hwpx')
        self.assertTrue(payload['working_copy_dirty'])
        self.assertEqual(payload['last_proof_artifact'], 'C:/work/page.png')
        self.assertFalse(payload['proof_fresh'])
        self.assertEqual(payload['proof_fresh_reason'], 'working copy has unsaved changes after the last known proof')
        self.assertTrue(payload['live_session_bound'])

    def test_clean_live_session_with_rendered_proof_reports_fresh(self) -> None:
        payload = build_local_cli_status_payload(
            snapshot={'ready': True, 'checks': {'hancom_automation': {'ok': True}}, 'errors': []},
            active_binding={
                'session_id': 's1',
                'source_filename': 'source.hwpx',
                'working_copy_dirty': False,
                'artifacts': {'latest_screenshot_path': 'C:/work/page.png'},
            },
            live_bound=True,
        )

        self.assertTrue(payload['proof_fresh'])
        self.assertEqual(payload['proof_fresh_reason'], 'latest proof is not known stale')

    def test_manifest_status_exposes_runtime_op_inventory(self) -> None:
        payload = build_local_cli_status_payload(
            snapshot={'ready': True, 'checks': {'hancom_automation': {'ok': True}}, 'errors': []},
            active_binding=None,
            live_bound=False,
            command_package_status={
                'ops': ['context', 'where'],
                'op_count': 2,
                'revision': 'context/manifest.json:1|where/manifest.json:2',
            },
        )

        self.assertEqual(payload['command_package_ops'], ['context', 'where'])
        self.assertEqual(payload['command_package_op_count'], 2)
        self.assertEqual(payload['command_package_revision'], 'context/manifest.json:1|where/manifest.json:2')

    def test_not_ready_runtime_reports_blocked_reason(self) -> None:
        payload = build_local_cli_status_payload(
            snapshot={
                'ready': False,
                'checks': {'hancom_automation': {'ok': False}},
                'errors': ['Hancom automation unavailable'],
            },
            active_binding=None,
            live_bound=False,
        )

        self.assertFalse(payload['runtime_up'])
        self.assertFalse(payload['hancom_attached'])
        self.assertEqual(payload['blocked_reason'], 'Hancom automation unavailable')
        self.assertEqual(payload['next_action'], 'restore runtime readiness on the Windows Hancom worker')


if __name__ == '__main__':
    unittest.main()
