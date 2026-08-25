from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import local_cli_v1.main as cli_main  # noqa: E402
from local_cli_v1.envelope import ENVELOPE_SCHEMA_VERSION, build_envelope  # noqa: E402


REQUIRED_TOP_LEVEL = ('schema_version', 'result', 'where', 'how', 'changed', 'proof', 'next', 'warnings', 'blocked_reason')
REQUIRED_HUMAN_KEYS = ('result:', 'where:', 'how:', 'changed:', 'proof:', 'next:')


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli_main.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def parse_json_output(label: str, output: str) -> dict:
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SystemExit(f'{label} did not print JSON: {exc}: {output!r}') from exc
    for key in REQUIRED_TOP_LEVEL:
        require(key in data, f'{label} missing top-level envelope field {key!r}')
    require(data['schema_version'] == ENVELOPE_SCHEMA_VERSION, f'{label} schema mismatch: {data.get("schema_version")!r}')
    proof = data.get('proof')
    require(isinstance(proof, dict), f'{label} proof must be object')
    artifact = proof.get('artifact')
    manifest = proof.get('manifest')
    require(isinstance(artifact, dict), f'{label} proof.artifact must be object')
    require(isinstance(manifest, dict), f'{label} proof.manifest must be object')
    require(artifact.get('role') == 'exported PDF proof source', f'{label} artifact role mismatch: {artifact!r}')
    require(artifact.get('path'), f'{label} artifact path missing')
    require(manifest.get('path'), f'{label} manifest path missing')
    require(isinstance(manifest.get('data'), dict), f'{label} manifest data missing')
    next_data = data.get('next')
    require(isinstance(next_data, dict) and next_data.get('review_instruction'), f'{label} review instruction missing')
    return data


def require_same_export_proof_meaning(direct: dict, bundle: dict) -> None:
    require(direct['result'] == bundle['result'] == 'ok', 'result parity mismatch')
    require(direct['changed'] == bundle['changed'], 'changed parity mismatch')
    require(direct['proof']['artifact']['role'] == bundle['proof']['artifact']['role'], 'artifact role parity mismatch')
    require(direct['proof']['manifest']['data']['schema_version'] == bundle['proof']['manifest']['data']['schema_version'], 'manifest schema parity mismatch')
    require(direct['proof']['manifest']['data']['kind'] == bundle['proof']['manifest']['data']['kind'] == 'export', 'manifest kind parity mismatch')
    require(direct['next']['review_instruction'] == bundle['next']['review_instruction'], 'next review instruction parity mismatch')
    require(bundle['proof']['manifest']['data'].get('server_primitive') == 'export_pdf', 'bundle proof did not record export_pdf primitive')


def main() -> int:
    original_post_json = cli_main.post_json
    original_download_to_path = cli_main.download_to_path
    original_load_state = cli_main.load_state
    original_save_state = cli_main.save_state
    original_artifact_destination = cli_main._artifact_destination
    state = {
        'base_url': 'http://fixture.local',
        'session_id': 'session-fixture',
        'source_filename': 'fixture.hwpx',
        'source_path': '/tmp/fixture.hwpx',
    }

    with tempfile.TemporaryDirectory(prefix='hwpx-envelope-parity-') as tmp_raw:
        tmp = Path(tmp_raw)
        destinations = iter(
            [
                tmp / 'direct.pdf',
                tmp / 'bundle.pdf',
                tmp / 'bundle-human.pdf',
            ]
        )

        def fake_load_state(path=None):  # noqa: ANN001
            return dict(state)

        def fake_save_state(new_state, path=None):  # noqa: ANN001
            state.clear()
            state.update(new_state)

        def fake_artifact_destination(kind: str, *, page: int | None = None) -> Path:
            require(kind == 'export', f'unexpected artifact kind in parity smoke: {kind}')
            return next(destinations)

        def fake_download_to_path(base_url: str, download_path: str, destination: Path) -> Path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f'%PDF-1.4 fixture {download_path}\n'.encode('utf-8'))
            return destination

        def fake_post_json(base_url: str, route: str, payload: dict):
            if route == '/local-cli/export':
                return {'download_path': '/direct/export.pdf', 'artifact_path': 'server/direct/export.pdf'}
            if route == '/local-cli/command-bundle':
                steps = payload.get('steps') if isinstance(payload, dict) else None
                require(isinstance(steps, list) and steps and steps[0].get('op') == 'export_pdf', 'bundle export did not send export_pdf step')
                return {
                    'summary': 'fixture bundle export ok',
                    'steps': [
                        {
                            'op': 'export_pdf',
                            'label': 'export:fresh-pdf',
                            'ok': True,
                            'result': {'download_path': '/bundle/export.pdf', 'artifact_path': 'server/bundle/export.pdf'},
                        }
                    ],
                }
            raise AssertionError(f'unexpected route: {route}')

        try:
            cli_main.post_json = fake_post_json
            cli_main.download_to_path = fake_download_to_path
            cli_main.load_state = fake_load_state
            cli_main.save_state = fake_save_state
            cli_main._artifact_destination = fake_artifact_destination

            rc, stdout, stderr = run_cli(['export', '--json'])
            require(rc == 0, f'direct export --json returned {rc}: {stderr}')
            direct = parse_json_output('direct export', stdout)

            rc, stdout, stderr = run_cli(['export', '--bundle-proof', '--json'])
            require(rc == 0, f'bundle export --json returned {rc}: {stderr}')
            bundle = parse_json_output('bundle export', stdout)
            require_same_export_proof_meaning(direct, bundle)

            rc, stdout, stderr = run_cli(['export', '--bundle-proof'])
            require(rc == 0, f'bundle export human returned {rc}: {stderr}')
            for key in REQUIRED_HUMAN_KEYS:
                require(key in stdout, f'bundle human output missing {key!r}: {stdout!r}')
            require('manifest:' in stdout, 'bundle human output missing manifest line')

            blocked = build_envelope(
                result='blocked',
                where='fixture.hwpx',
                how='static contract sample',
                changed='none',
                proof='none',
                next_step='reload Windows API only when Jun explicitly asks',
                warnings=['static sample only'],
                blocked_reason='running Windows API not checked in static smoke',
            )
            require(blocked['blocked_reason'], 'blocked_reason structural support missing')
            require(blocked['warnings'], 'warning structural support missing')
        finally:
            cli_main.post_json = original_post_json
            cli_main.download_to_path = original_download_to_path
            cli_main.load_state = original_load_state
            cli_main.save_state = original_save_state
            cli_main._artifact_destination = original_artifact_destination

    print('ok: cli JSON envelope export parity static smoke')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
