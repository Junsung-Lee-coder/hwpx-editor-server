from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_cli_v1 import main as cli_main  # noqa: E402
from local_cli_v1.transport import ApiError  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def capture_status_output(payload: dict) -> str:
    out = io.StringIO()
    with patch.object(cli_main, 'load_state', return_value={'session_id': 'cached-session'}):
        with contextlib.redirect_stdout(out):
            cli_main._print_status(payload)
    return out.getvalue()


def require_route_probe_contract() -> None:
    with patch.object(cli_main, '_state_session_id', return_value='cached-session'), patch.object(
        cli_main,
        'post_json',
        side_effect=ApiError('command-bundle steps must not be empty', status_code=400),
    ):
        active = cli_main._probe_command_bundle_route('http://server')
    require(active.get('command_bundle_route_active') is True, '400 validation probe must mean mounted/active route')
    require(active.get('probe_status') == 'active-validation-error', f'wrong active probe status: {active!r}')
    require(bool(active.get('route_error')), f'active probe must preserve validation detail: {active!r}')

    with patch.object(cli_main, 'post_json', side_effect=ApiError('not found', status_code=404)):
        missing = cli_main._probe_command_bundle_route('http://server')
    require(missing.get('command_bundle_route_active') is False, '404 probe must mean unavailable route')
    require(missing.get('probe_status') == 'error-404', f'wrong unavailable probe status: {missing!r}')
    require(missing.get('route_error') == 'not found', f'unavailable probe must expose error detail: {missing!r}')


def require_status_visibility_output() -> None:
    output = capture_status_output(
        {
            'runtime_up': True,
            'hancom_attached': True,
            'api_ready': True,
            'command_bundle_route_active': False,
            'probe_status': 'error-404',
            'route_error': 'not found',
            'server_primitive_version': 'local-cli-command-bundle/v2-style-inspect',
        }
    )
    for needle in (
        'how: direct /local-cli/status plus non-mutating command-bundle route probe',
        'command-bundle route: unavailable',
        'command-bundle probe: error-404',
        'route error: not found',
        'server primitives: local-cli-command-bundle/v2-style-inspect',
    ):
        require(needle in output, f'status output missing {needle!r}:\n{output}')


def main() -> int:
    require_route_probe_contract()
    require_status_visibility_output()
    print('ok: local CLI health/status visibility smoke passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
