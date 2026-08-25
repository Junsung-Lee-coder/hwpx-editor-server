from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from local_cli_v1 import main as cli_main
from local_cli_v1.transport import ApiError


class LocalCliStatusHealthTests(unittest.TestCase):
    def test_probe_command_bundle_route_classifies_validation_error_as_active(self) -> None:
        with patch.object(cli_main, '_state_session_id', return_value='session-1'), patch.object(
            cli_main,
            'post_json',
            side_effect=ApiError('command-bundle steps must not be empty', status_code=400),
        ) as post_json:
            result = cli_main._probe_command_bundle_route('http://server')

        post_json.assert_called_once_with(
            'http://server',
            '/local-cli/command-bundle',
            {'steps': [], 'session_id': 'session-1'},
        )
        self.assertTrue(result['command_bundle_route_active'])
        self.assertEqual(result['probe_status'], 'active-validation-error')
        self.assertEqual(result['route_error'], 'command-bundle steps must not be empty')
        self.assertIn('server_primitive_version', result)

    def test_probe_command_bundle_route_exposes_unavailable_error_details(self) -> None:
        with patch.object(cli_main, 'post_json', side_effect=ApiError('not found', status_code=404)):
            result = cli_main._probe_command_bundle_route('http://server')

        self.assertFalse(result['command_bundle_route_active'])
        self.assertEqual(result['probe_status'], 'error-404')
        self.assertEqual(result['route_error'], 'not found')

    def test_print_status_includes_route_probe_details(self) -> None:
        payload = {
            'runtime_up': True,
            'hancom_attached': True,
            'api_ready': True,
            'command_bundle_route_active': False,
            'probe_status': 'error-404',
            'route_error': 'not found',
            'server_primitive_version': 'local-cli-command-bundle/v2-style-inspect',
            'command_package_op_count': 2,
            'command_package_ops': ['context', 'where'],
            'command_package_revision': 'context/manifest.json:1|where/manifest.json:2',
            'next_action': 'open a file',
        }
        with patch.object(cli_main, 'load_state', return_value={'session_id': 'cached-session'}):
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main._print_status(payload)

        text = out.getvalue()
        self.assertIn('command-bundle route: unavailable', text)
        self.assertIn('command-bundle probe: error-404', text)
        self.assertIn('route error: not found', text)
        self.assertIn('server primitives: local-cli-command-bundle/v2-style-inspect', text)
        self.assertIn('command packages: 2 ops', text)
        self.assertIn('command package sample: context, where', text)
        self.assertIn('command package revision: context/manifest.json:1|where/manifest.json:2', text)

    def test_print_session_health_includes_probe_status_and_route_error(self) -> None:
        with patch.object(cli_main, 'load_state', return_value={'session_id': 'cached-session'}), patch.object(
            cli_main,
            'get_json',
            return_value={'runtime_up': True, 'live_session_bound': False},
        ), patch.object(
            cli_main,
            '_probe_command_bundle_route',
            return_value={
                'command_bundle_route_active': False,
                'probe_status': 'error-unreachable',
                'route_error': 'connection refused',
            },
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main._print_session_health('http://server')

        text = out.getvalue()
        self.assertIn('command-bundle route: unavailable', text)
        self.assertIn('command-bundle probe: error-unreachable', text)
        self.assertIn('route error: connection refused', text)


if __name__ == '__main__':
    unittest.main()
