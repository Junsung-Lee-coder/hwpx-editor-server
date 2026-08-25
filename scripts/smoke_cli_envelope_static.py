from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import local_cli_v1.main as cli_main  # noqa: E402
from local_cli_v1.envelope import build_envelope  # noqa: E402


REQUIRED_KEYS = ('result:', 'where:', 'how:', 'changed:', 'proof:', 'next:')


def capture(fn, *args, **kwargs) -> str:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        fn(*args, **kwargs)
    return stdout.getvalue()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def require_envelope(label: str, output: str) -> None:
    for key in REQUIRED_KEYS:
        require(key in output, f'{label} missing envelope key {key!r}: {output!r}')


def require_value_error(label: str, **kwargs) -> None:
    try:
        build_envelope(**kwargs)
    except ValueError as exc:
        require('blocked_reason' in str(exc), f'{label} raised unexpected ValueError: {exc}')
        return
    raise SystemExit(f'{label} should fail blocked envelope validation')


def main() -> int:
    original_load_state = cli_main.load_state
    try:
        cli_main.load_state = lambda: {
            'source_filename': 'fixture.hwpx',
            'session_id': 'session-fixture',
            'last_page_screenshot_path': '/tmp/fixture-page-001.png',
            'last_page_screenshot_manifest_path': '/tmp/fixture-page-001.manifest.json',
        }
        status_output = capture(
            cli_main._print_status,
            {
                'runtime_up': True,
                'hancom_attached': True,
                'api_ready': True,
                'live_session_bound': True,
                'working_copy_dirty': False,
                'blocked_reason': None,
                'next_action': 'continue proof review',
                'command_bundle_route_active': False,
            },
        )
    finally:
        cli_main.load_state = original_load_state
    require_envelope('status', status_output)
    require('changed: none' in status_output, 'status should report no mutation')
    require('command-bundle route: unavailable' in status_output, 'status route probe line missing')

    artifact_output = capture(
        cli_main._print_artifact_result,
        role='rendered page proof',
        path=Path('/tmp/fixture-page-001.png'),
        manifest_path=Path('/tmp/fixture-page-001.manifest.json'),
        next_step='review rendered proof',
    )
    require_envelope('artifact', artifact_output)
    require('manifest: /tmp/fixture-page-001.manifest.json' in artifact_output, 'artifact manifest line missing')

    lifecycle_output = capture(
        cli_main._print_lifecycle_result,
        where='fixture.hwpx; session=session-fixture',
        how='direct lifecycle route',
        changed='live session opened/bound; original source file untouched',
        proof='none yet',
        next_step='run status',
    )
    require_envelope('lifecycle', lifecycle_output)
    require('original source file untouched' in lifecycle_output, 'lifecycle original-file guard missing')

    blocked_kwargs = {
        'result': 'blocked',
        'where': 'fixture.hwpx',
        'how': 'static envelope validation sample',
        'changed': 'none',
        'proof': 'none',
        'next_step': 'report the blocker and do not restart without explicit approval',
    }
    valid_blocked = build_envelope(
        **blocked_kwargs,
        blocked_reason='command-bundle route unavailable in running Windows API',
    )
    require(valid_blocked['result'] == 'blocked', 'valid blocked envelope result changed')
    require(valid_blocked['blocked_reason'], 'valid blocked envelope missing blocked_reason')
    require_value_error('blocked without reason', **blocked_kwargs)
    require_value_error('blocked with empty reason', **blocked_kwargs, blocked_reason='   ')

    print('ok: cli envelope static smoke')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
