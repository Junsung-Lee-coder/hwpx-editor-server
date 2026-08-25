from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.command_packages.runtime import get_command_package_registry  # noqa: E402
from local_cli_v1.bundles import BundleError, build_named_bundle, parse_pipe_table  # noqa: E402
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


def require_parser_and_bundle_shape() -> dict[str, object]:
    table = '| Item | Value |\n|---|---|\n| water | 10 kg |\n| fiber | 2.5 mm |'
    matrix = parse_pipe_table(table)
    require(matrix == [['Item', 'Value'], ['water', '10 kg'], ['fiber', '2.5 mm']], f'parsed matrix drifted: {matrix!r}')

    spec = build_named_bundle('text-table-to-native', ['--text', table, '--confirm-native-table'])
    payload = spec.server_payload()
    require([step.get('op') for step in payload['steps']] == ['native_table_insert'], f'wrong ops: {payload!r}')
    step = payload['steps'][0]
    require(step.get('rows') == 3 and step.get('cols') == 2, f'rows/cols wrong: {step!r}')
    require(step.get('cells') == matrix, f'cells changed: {step!r}')
    require('10 kg' in step.get('cells')[1] and '2.5 mm' in step.get('cells')[2], f'numeric/unit tokens not preserved: {step!r}')
    require(step.get('flat_value_hashes') == [_hash(value) for row in matrix for value in row], f'hashes mismatch: {step!r}')
    require(step.get('non_empty_token_count') == 6, f'non-empty count wrong: {step!r}')
    require(step.get('source_text_deleted') is False, f'source deletion flag wrong: {step!r}')
    require(step.get('old_plain_text_removal') == 'deferred_until_rendered_proof', f'cleanup boundary wrong: {step!r}')
    require('rendered proof' in str(step.get('next_proof_required')), f'proof requirement missing: {step!r}')

    status = build_command_status(build_parser()).get('text-table-to-native')
    require(status and status.get('status') == 'bundle-backed', f'command-status classification wrong: {status!r}')
    require('rendered proof' in str(status.get('note')) and 'no source text deletion' in str(status.get('note')), f'command-status safety note missing: {status!r}')
    return step


def require_split_by_column_bundle_shape() -> None:
    table = '| side | treatment | value |\n|---|---|---|\n| p-side | co-c | 47.199 |\n| t-side | co-c | 62.634 |\n| p-side | co-po1 | 45.937 |'
    spec = build_named_bundle('text-table-to-native', ['--text', table, '--split-by-column', 'side', '--confirm-native-table'])
    payload = spec.server_payload()
    steps = payload['steps']
    require([step.get('op') for step in steps] == ['native_table_insert', 'native_table_insert'], f'split ops wrong: {payload!r}')
    first, second = steps
    require(first.get('label') == 'native-table:split-001', f'first split label wrong: {first!r}')
    require(second.get('label') == 'native-table:split-002', f'second split label wrong: {second!r}')
    split_metadata_keys = {'split_by_column', 'split_column_index', 'split_group_value', 'split_group_index', 'split_group_count', 'split_group_hash'}
    require(not (split_metadata_keys & set(first)), f'split metadata leaked into first server step: {first!r}')
    require(not (split_metadata_keys & set(second)), f'split metadata leaked into second server step: {second!r}')
    require(first.get('cells') == [['side', 'treatment', 'value'], ['p-side', 'co-c', '47.199'], ['p-side', 'co-po1', '45.937']], f'first split cells wrong: {first!r}')
    require(second.get('cells') == [['side', 'treatment', 'value'], ['t-side', 'co-c', '62.634']], f'second split cells wrong: {second!r}')
    require(first.get('flat_value_hashes') == [_hash(value) for row in first['cells'] for value in row], f'first split hashes mismatch: {first!r}')
    require('split table 1/2' in str(first.get('next_proof_required')), f'split proof requirement missing: {first!r}')
    debug = spec.debug_payload()
    sources = debug.get('sources') or []
    require(len(sources) == 2 and sources[0].get('split_group_hash'), f'split sources missing metadata: {debug!r}')
    require(sources[0].get('split_by_column') == 'side' and sources[0].get('split_column_index') == 0, f'split source column metadata wrong: {debug!r}')
    require(sources[0].get('split_group_value') == 'p-side' and sources[1].get('split_group_value') == 't-side', f'split source group order wrong: {debug!r}')
    require(sources[0].get('split_group_index') == 1 and sources[0].get('split_group_count') == 2, f'split source count wrong: {debug!r}')

    try:
        build_named_bundle('text-table-to-native', ['--text', table, '--split-by-column', 'missing', '--confirm-native-table'])
    except BundleError:
        pass
    else:
        raise SystemExit('split-by-column must fail closed for missing header')

    try:
        build_named_bundle('text-table-to-native', ['--text', '| side | v |\n|---|---|\n|  | 1 |\n| p | 2 |', '--split-by-column', 'side', '--confirm-native-table'])
    except BundleError:
        pass
    else:
        raise SystemExit('split-by-column must fail closed for empty group values')


def require_fail_closed_validation(good_step: dict[str, object]) -> None:
    try:
        parse_pipe_table('| A | B |\n|---|---|\n| 1 | 2 | 3 |')
    except BundleError:
        pass
    else:
        raise SystemExit('non-rectangular pipe table must fail closed')

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            build_named_bundle('text-table-to-native', ['--text', '| A | B |'])
        except BundleError:
            pass
        else:
            raise SystemExit('text-table-to-native must require --confirm-native-table')

    package = get_command_package_registry().get('native_table_insert')
    require(package is not None, 'native_table_insert package missing')
    bad_step = dict(good_step)
    bad_step.pop('confirm_native_table', None)
    try:
        package.validate(service=_ValidationService(), index=1, step=bad_step, error_type=_ValidationError)
    except _ValidationError:
        pass
    else:
        raise SystemExit('package validation must reject missing confirm_native_table')

    bad_hash = dict(good_step)
    bad_hash['flat_value_hashes'] = ['sha256:bad']
    try:
        package.validate(service=_ValidationService(), index=1, step=bad_hash, error_type=_ValidationError)
    except _ValidationError:
        pass
    else:
        raise SystemExit('package validation must reject mismatched value hashes')


class _FakeHwp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_table(self, rows: int, cols: int, treat_as_char: bool) -> bool:
        self.calls.append(('create_table', [rows, cols, treat_as_char]))
        return True

    def TableCellBlockExtendAbs(self) -> bool:
        self.calls.append(('TableCellBlockExtendAbs', []))
        return True

    def TableCellBlockExtend(self) -> bool:
        self.calls.append(('TableCellBlockExtend', []))
        return True

    def set_cur_field_name(self, field_name: str) -> bool:
        self.calls.append(('set_cur_field_name', field_name))
        return True

    def put_field_text(self, field_name: str, values: list[str]) -> bool:
        self.calls.append(('put_field_text', [field_name, list(values)]))
        return True

    def Cancel(self) -> bool:
        self.calls.append(('Cancel', []))
        return True


class _FakeHandle:
    def __init__(self) -> None:
        self.hwp = _FakeHwp()


class _FakeService:
    def _bundle_compact_snapshot(self, hwp: object) -> dict[str, object]:
        return {'fake': True}


def require_native_runtime_sequence(step: dict[str, object]) -> None:
    runtime_source = (ROOT / 'app' / 'local_cli_runtime.py').read_text(encoding='utf-8')
    for needle in (
        'create_table_at_cursor(hwp, cols=cols, rows=rows)',
        "_call_required_hwp_method(hwp, 'TableCellBlockExtendAbs')",
        "_call_required_hwp_method(hwp, 'TableCellBlockExtend')",
        "_call_required_hwp_method(hwp, 'set_cur_field_name', field_name)",
        "_call_required_hwp_method(hwp, 'put_field_text', field_name, flat_values)",
        "_call_required_hwp_method(hwp, 'set_cur_field_name', '')",
    ):
        require(needle in runtime_source, f'runtime source missing native table sequence: {needle}')
    native_function = runtime_source.split('def insert_native_table_at_cursor', 1)[1].split('\n\ndef ', 1)[0]
    forbidden = ('Delete(', 'Erase(', 'cell_replace', 'raw_xml', 'package mutation')
    require(not any(token in native_function for token in forbidden), f'runtime native table function contains forbidden cleanup/raw token')

    package = get_command_package_registry().get('native_table_insert')
    require(package is not None, 'native_table_insert package missing')
    handle = _FakeHandle()

    def fake_insert_native_table_at_cursor(hwp: object, *, rows: int, cols: int, cells: list[list[str]], field_name: str) -> dict[str, object]:
        hwp.create_table(rows, cols, True)
        hwp.TableCellBlockExtendAbs()
        hwp.TableCellBlockExtend()
        hwp.set_cur_field_name(field_name)
        hwp.put_field_text(field_name, [value for row in cells for value in row])
        hwp.set_cur_field_name('')
        hwp.Cancel()
        return {
            'source_text_deleted': False,
            'old_plain_text_removal': 'deferred_until_rendered_proof',
            'rows': rows,
            'cols': cols,
            'warnings': [],
        }

    fake_runtime = types.SimpleNamespace(
        LocalCliRuntimeError=RuntimeError,
        export_document_pdf=lambda **_kwargs: Path('fake.pdf'),
        insert_native_table_at_cursor=fake_insert_native_table_at_cursor,
        save_document=lambda _hwp: None,
        snapshot_live_location=lambda **_kwargs: {},
    )
    old_runtime = sys.modules.get('app.local_cli_runtime')
    sys.modules['app.local_cli_runtime'] = fake_runtime
    try:
        result, dirty, warnings = package.run(service=_FakeService(), handle=handle, step=dict(step), binding=None)
    finally:
        if old_runtime is None:
            sys.modules.pop('app.local_cli_runtime', None)
        else:
            sys.modules['app.local_cli_runtime'] = old_runtime
    require(dirty is True, 'native_table_insert must mark the working copy dirty')
    require(result.get('source_text_deleted') is False, f'runtime source deletion flag wrong: {result!r}')
    require(result.get('old_plain_text_removal') == 'deferred_until_rendered_proof', f'runtime cleanup boundary wrong: {result!r}')
    require(any('Old pipe/plain source text cleanup is deferred' in warning for warning in warnings), f'safety warning missing: {warnings!r}')
    call_names = [name for name, _args in handle.hwp.calls]
    require(
        call_names == ['create_table', 'TableCellBlockExtendAbs', 'TableCellBlockExtend', 'set_cur_field_name', 'put_field_text', 'set_cur_field_name', 'Cancel'],
        f'native runtime call order drifted: {handle.hwp.calls!r}',
    )
    put_call = handle.hwp.calls[4][1]
    require(put_call[1] == [value for row in step['cells'] for value in row], f'row-major fill values wrong: {handle.hwp.calls!r}')


def require_cli_human_safety_output() -> None:
    response = {
        'ok': True,
        'command': 'command-bundle',
        'steps': [
            {
                'index': 1,
                'label': 'native-table:insert-and-fill',
                'op': 'native_table_insert',
                'ok': True,
                'result': {'rows': 1, 'cols': 2, 'source_text_deleted': False, 'old_plain_text_removal': 'deferred_until_rendered_proof'},
            }
        ],
        'warnings': ['rendered proof required'],
    }
    stdout = io.StringIO()
    captured: dict[str, object] = {}

    def fake_post_json(base_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        captured['path'] = path
        captured['payload'] = payload
        return response

    with patch('local_cli_v1.main.post_json', fake_post_json), contextlib.redirect_stdout(stdout):
        rc = cli_main(['text-table-to-native', '--text', '| A | B |', '--confirm-native-table'])
    output = stdout.getvalue()
    require(rc == 0, f'CLI returned non-zero: {rc}')
    require(captured.get('path') == '/local-cli/command-bundle', f'CLI used wrong route: {captured!r}')
    steps = captured.get('payload', {}).get('steps', [])
    require(steps and steps[0].get('op') == 'native_table_insert', f'CLI did not build native_table_insert: {captured!r}')
    require('old pipe/plain text was not deleted' in output, f'human safety output missing old-text boundary:\n{output}')
    require('run rendered proof before save or any old text cleanup' in output, f'human safety output missing proof requirement:\n{output}')

    stdout = io.StringIO()
    captured = {}
    with patch('local_cli_v1.main.post_json', fake_post_json), contextlib.redirect_stdout(stdout):
        rc = cli_main(['text-table-to-native', '--text', '| side | value |\n|---|---|\n| p | 1 |\n| t | 2 |', '--split-by-column', 'side', '--confirm-native-table'])
    output = stdout.getvalue()
    require(rc == 0, f'split CLI returned non-zero: {rc}')
    split_steps = captured.get('payload', {}).get('steps', [])
    split_metadata_keys = {'split_by_column', 'split_column_index', 'split_group_value', 'split_group_index', 'split_group_count', 'split_group_hash'}
    require(len(split_steps) == 2 and split_steps[0].get('cells') == [['side', 'value'], ['p', '1']], f'CLI did not build split native tables: {captured!r}')
    require(not any(split_metadata_keys & set(step) for step in split_steps), f'CLI leaked split metadata to server payload: {captured!r}')
    require('split native table insertion bundle executed' in output, f'split human output missing split boundary:\n{output}')
    require('verify every split table with rendered proof' in output, f'split human output missing split proof requirement:\n{output}')


def main() -> int:
    step = require_parser_and_bundle_shape()
    require_split_by_column_bundle_shape()
    require_fail_closed_validation(step)
    require_native_runtime_sequence(step)
    require_cli_human_safety_output()
    print('ok: native table command static smoke')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
