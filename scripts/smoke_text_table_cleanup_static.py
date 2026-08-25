from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.command_packages.runtime import get_command_package_registry  # noqa: E402
from local_cli_v1.bundles import BundleError, build_named_bundle  # noqa: E402
from local_cli_v1.main import build_command_status, build_parser, main as cli_main  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


class _ValidationError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class _ValidationService:
    pass


def _hash(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def _normalized(value: str) -> str:
    return ' '.join(value.split()).strip()


def require_bundle_shape() -> dict[str, object]:
    source = '| A | B |\n|---|---|\n| old | 1 kg |'
    spec = build_named_bundle('text-table-cleanup-selected', ['--text', source, '--confirm-cleanup'])
    payload = spec.server_payload()
    require([step.get('op') for step in payload['steps']] == ['selected_text_delete_exact'], f'wrong ops: {payload!r}')
    step = payload['steps'][0]
    require(step.get('expected_text') == source, f'expected_text changed: {step!r}')
    require(step.get('expected_hash') == _hash(source), f'exact source hash mismatch: {step!r}')
    require(step.get('expected_normalized_hash') == _hash(_normalized(source)), f'normalized hash mismatch: {step!r}')
    require(step.get('confirm_cleanup') is True, f'cleanup confirm missing: {step!r}')
    require(step.get('source_text_deleted') is False, f'pre-runtime deletion proof must be false: {step!r}')

    native = build_named_bundle('text-table-to-native', ['--text', source, '--confirm-native-table']).server_payload()['steps'][0]
    require(native.get('op') == 'native_table_insert', f'native command op drifted: {native!r}')
    require(native.get('source_text_deleted') is False, f'text-table-to-native must not delete source text: {native!r}')
    require(native.get('old_plain_text_removal') == 'deferred_until_rendered_proof', f'native cleanup boundary drifted: {native!r}')

    status = build_command_status(build_parser()).get('text-table-cleanup-selected')
    require(status and status.get('status') == 'bundle-backed', f'command-status classification wrong: {status!r}')
    require('table/cell' in str(status.get('note')) and 'rendered' in str(status.get('note')), f'command-status safety note missing: {status!r}')
    return step


def require_fail_closed_validation(good_step: dict[str, object]) -> None:
    for argv, message in (
        (['--text', 'old source'], 'missing confirm must fail'),
        (['--text', '', '--confirm-cleanup'], 'empty source text must fail'),
        (['--text', 'old source', '--expected-hash', _hash('different'), '--confirm-cleanup'], 'hash mismatch must fail'),
    ):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                build_named_bundle('text-table-cleanup-selected', argv)
            except BundleError:
                pass
            else:
                raise SystemExit(message)

    package = get_command_package_registry().get('selected_text_delete_exact')
    require(package is not None, 'selected_text_delete_exact package missing')
    require('cell_text' not in package.allowed_keys and 'replacement_text' not in package.allowed_keys, f'package exposes table-cell/broad replacement keys: {package.allowed_keys!r}')

    for bad_step, message in (
        ({**good_step, 'confirm_cleanup': False}, 'package validation must reject missing confirm_cleanup'),
        ({**good_step, 'expected_text': ''}, 'package validation must reject empty expected_text'),
        ({**good_step, 'expected_hash': _hash('different')}, 'package validation must reject expected_hash mismatch'),
        ({**good_step, 'source_text_deleted': True}, 'package validation must reject pre-runtime source_text_deleted=true'),
    ):
        try:
            package.validate(service=_ValidationService(), index=1, step=dict(bad_step), error_type=_ValidationError)
        except _ValidationError:
            pass
        else:
            raise SystemExit(message)


class _FakeHAction:
    def __init__(self, hwp: '_FakeHwp') -> None:
        self.hwp = hwp
        self.calls: list[str] = []

    def Run(self, action_name: str) -> bool:
        self.calls.append(action_name)
        if action_name == 'Delete':
            self.hwp.deleted = True
            self.hwp.selected_text = ''
            self.hwp.has_selection = False
            return True
        return False


class _FakeHwp:
    def __init__(self, *, selected_text: str = 'old source', has_selection: bool = True, is_cell: bool = False, cell_addr: str | None = None, selection_mode: object = 0) -> None:
        self.selected_text = selected_text
        self.has_selection = has_selection
        self._is_cell = is_cell
        self._cell_addr = cell_addr
        self._selection_mode = selection_mode
        self.deleted = False
        self.HAction = _FakeHAction(self)

    def get_pos(self) -> tuple[int, int, int]:
        return (0, 1, 2)

    def get_selected_pos(self) -> tuple[object, ...]:
        return (self.has_selection, 0, 1, 0, 0, 1, len(self.selected_text))

    def get_selected_text(self, *, keep_select: bool = True) -> str:
        return self.selected_text if self.has_selection else ''

    def get_text_file(self, option: str = '') -> str:
        return f'before {self.selected_text} after'

    def is_cell(self) -> bool:
        return self._is_cell

    def get_cell_addr(self) -> str | None:
        return self._cell_addr

    def SelectionMode(self) -> object:
        return self._selection_mode


class _FakeHandle:
    def __init__(self, hwp: _FakeHwp) -> None:
        self.hwp = hwp


def require_runtime_gates(good_step: dict[str, object]) -> None:
    package = get_command_package_registry().get('selected_text_delete_exact')
    require(package is not None, 'selected_text_delete_exact package missing')
    runtime_step = build_named_bundle('text-table-cleanup-selected', ['--text', 'old source', '--confirm-cleanup']).server_payload()['steps'][0]
    validated = package.validate(service=_ValidationService(), index=1, step=dict(runtime_step), error_type=_ValidationError)

    hwp = _FakeHwp(selected_text='old source')
    result, dirty, warnings = package.run(service=_ValidationService(), handle=_FakeHandle(hwp), step=validated, binding=None)
    require(dirty is True and hwp.deleted is True, f'runtime did not delete exact selected text: result={result!r}')
    require(result.get('source_text_deleted') is True, f'runtime deletion proof missing: {result!r}')
    require(result.get('expected_hash') == _hash('old source'), f'expected hash missing in proof: {result!r}')
    require(result.get('selected_text', {}).get('hash') == _hash('old source'), f'selected hash missing in proof: {result!r}')
    require(any('Rendered before/after proof' in warning for warning in warnings), f'rendered proof warning missing: {warnings!r}')

    normalized_step = build_named_bundle('text-table-cleanup-selected', ['--text', 'old\nsource', '--confirm-cleanup']).server_payload()['steps'][0]
    normalized_step = package.validate(service=_ValidationService(), index=1, step=dict(normalized_step), error_type=_ValidationError)

    for hwp_bad, message, step_for_case in (
        (_FakeHwp(selected_text='old source', has_selection=False), 'runtime must reject missing active selection', validated),
        (_FakeHwp(selected_text='different'), 'runtime must reject selected text/hash mismatch', validated),
        (_FakeHwp(selected_text='old source'), 'runtime must reject normalized-only selected text/hash mismatch', normalized_step),
        (_FakeHwp(selected_text='old source', is_cell=True, cell_addr='A1'), 'runtime must reject table/cell context', validated),
        (_FakeHwp(selected_text='old source', selection_mode=3), 'runtime must reject table selection mode', validated),
    ): 
        try:
            package.run(service=_ValidationService(), handle=_FakeHandle(hwp_bad), step=dict(step_for_case), binding=None)
        except Exception:
            pass
        else:
            raise SystemExit(message)


def require_static_source_boundaries() -> None:
    source = (ROOT / 'app' / 'command_packages' / 'commands' / 'selected_text_delete_exact' / 'run.py').read_text(encoding='utf-8')
    for needle in ('raw_xml', 'zipfile', 'cell_replace', '_clear_current_cell_text', '_select_current_cell_contents'):
        require(needle not in source, f'cleanup runtime contains forbidden raw/table-cell path token: {needle}')
    require('_delete_selection(hwp)' in source, 'cleanup runtime must use the existing selected-range delete helper only after gates')


def require_cli_human_safety_output() -> None:
    response = {
        'ok': True,
        'command': 'command-bundle',
        'steps': [
            {
                'index': 1,
                'label': 'cleanup:selected-text-delete-exact',
                'op': 'selected_text_delete_exact',
                'ok': True,
                'result': {'source_text_deleted': True, 'rendered_before_after_proof_required': True},
            }
        ],
        'warnings': ['Rendered before/after proof is still required'],
    }
    stdout = io.StringIO()
    captured: dict[str, object] = {}

    def fake_post_json(base_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        captured['path'] = path
        captured['payload'] = payload
        return response

    with patch('local_cli_v1.main.post_json', fake_post_json), contextlib.redirect_stdout(stdout):
        rc = cli_main(['text-table-cleanup-selected', '--text', 'old source', '--confirm-cleanup'])
    output = stdout.getvalue()
    require(rc == 0, f'CLI returned non-zero: {rc}')
    require(captured.get('path') == '/local-cli/command-bundle', f'CLI used wrong route: {captured!r}')
    steps = captured.get('payload', {}).get('steps', [])
    require(steps and steps[0].get('op') == 'selected_text_delete_exact', f'CLI did not build cleanup op: {captured!r}')
    require(steps[0].get('source_text_deleted') is False, f'CLI must send source_text_deleted=false before runtime proof: {captured!r}')
    require('selected old source text deleted' in output, f'human safety output missing cleanup boundary:\n{output}')
    require('rendered before/after proof before save' in output, f'human safety output missing proof requirement:\n{output}')


def main() -> int:
    step = require_bundle_shape()
    require_fail_closed_validation(step)
    require_runtime_gates(step)
    require_static_source_boundaries()
    require_cli_human_safety_output()
    print('ok: text table cleanup selected static smoke')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
