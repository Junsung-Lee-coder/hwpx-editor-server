from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_cli_v1.bundles import BUNDLE_SERVER_OPS, BundleError, _STEP_KEYS, build_created_bundle, build_named_bundle, recipe_names  # noqa: E402
from local_cli_v1.main import _load_bundle_payload, _load_tx_server_payload, main as cli_main  # noqa: E402


def require_contains(path: Path, needle: str) -> None:
    text = path.read_text(encoding='utf-8')
    if needle not in text:
        raise SystemExit(f'missing {needle!r} in {path.relative_to(ROOT)}')


def require_not_contains(path: Path, needle: str) -> None:
    text = path.read_text(encoding='utf-8')
    if needle in text:
        raise SystemExit(f'unexpected domain-specific server op {needle!r} in {path.relative_to(ROOT)}')


def _literal_string_set_assignment(path: Path, name: str) -> set[str]:
    module = ast.parse(path.read_text(encoding='utf-8'))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, (set, frozenset)) or not all(isinstance(item, str) for item in value):
            raise SystemExit(f'{name} in {path.relative_to(ROOT)} is not a literal string set')
        return set(value)
    raise SystemExit(f'{name} assignment not found in {path.relative_to(ROOT)}')


def _literal_allowed_key_ops(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(module):
        value_node = None
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == 'allowed_keys' for target in node.targets):
                value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == 'allowed_keys':
                value_node = node.value
        if value_node is None:
            continue
        value = ast.literal_eval(value_node)
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise SystemExit('allowed_keys is not a literal dict keyed by op name')
        return set(value)
    raise SystemExit('allowed_keys assignment not found in app/local_cli_service.py')


def require_bundle_op_drift_check() -> None:
    server_path = ROOT / 'app' / 'local_cli_service.py'
    server_ops = _literal_string_set_assignment(server_path, '_BUNDLE_ALLOWED_OPS')
    server_validated_ops = _literal_allowed_key_ops(server_path)
    local_ops = set(BUNDLE_SERVER_OPS)
    local_key_ops = set(_STEP_KEYS)
    if local_ops != local_key_ops:
        raise SystemExit(f'local BUNDLE_SERVER_OPS/_STEP_KEYS drift: ops={sorted(local_ops)} keys={sorted(local_key_ops)}')
    missing_on_server = sorted(local_ops - server_ops)
    if missing_on_server:
        raise SystemExit(f'local bundle ops missing from server _BUNDLE_ALLOWED_OPS: {missing_on_server}')
    missing_server_validation = sorted(local_ops - server_validated_ops)
    if missing_server_validation:
        raise SystemExit(f'local bundle ops missing from server allowed_keys validation: {missing_server_validation}')
    server_backlog = server_ops - local_ops
    expected_server_backlog = {
        'save_document',
        'paragraph_join_previous_exact',
        'paragraph_join_next_exact',
        'control_join_previous_exact',
        'paragraph_rehome_exact',
    }
    if server_backlog != expected_server_backlog:
        raise SystemExit(
            'server/local bundle op drift changed; update local BUNDLE_SERVER_OPS or the documented direct-backlog set: '
            f'actual={sorted(server_backlog)} expected={sorted(expected_server_backlog)}'
        )


def require_bundle_shape() -> None:
    expected = {
        'where',
        'selection-proof',
        'selected-text-proof',
        'insert-text-file',
        'insert-before-anchor',
        'insert-after-anchor',
        'insert-after-paragraph',
        'insert-before-heading',
        'cell-proof',
        'section-control-inventory',
        'export-proof-range',
        'section-frame-fill',
        'section-graphic-remove-or-hide',
        'section-control-delete-exact',
        'exact-control-select-proof',
        'cell-format-exact',
        'table-cell-structure-exact',
        'paragraph-style-apply-exact',
        'text-table-to-native',
    }
    missing = expected - set(recipe_names())
    if missing:
        raise SystemExit(f'missing local bundle recipes: {sorted(missing)}')

    where_payload = build_named_bundle('where').server_payload()
    if set(where_payload) != {'steps'}:
        raise SystemExit('where bundle server payload must contain only steps')
    if where_payload['steps'] != [{'op': 'where', 'label': 'where:current-location'}]:
        raise SystemExit(f'unexpected where bundle payload: {where_payload!r}')

    selected_payload = build_named_bundle('selected-text-proof').server_payload()
    for local_only_key in ('where', 'how', 'changed', 'summary', 'server_payload'):
        if local_only_key in selected_payload:
            raise SystemExit(f'local metadata leaked into server payload top level: {local_only_key}')
        for step in selected_payload['steps']:
            if local_only_key in step:
                raise SystemExit(f'local metadata leaked into server step: {local_only_key}')
    ops = [step.get('op') for step in selected_payload['steps']]
    if ops != ['where', 'get_selected_text', 'where']:
        raise SystemExit(f'unexpected selected-text-proof ops: {ops!r}')

    selection_payload = build_named_bundle('selection-proof').server_payload()
    if selection_payload['steps'] != [{'op': 'selection_proof', 'label': 'selection-proof:active-selection'}]:
        raise SystemExit(f'unexpected selection-proof payload: {selection_payload!r}')

    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as handle:
        handle.write('bundle smoke text')
        text_path = Path(handle.name)
    try:
        insert_payload = build_named_bundle('insert-text-file', [str(text_path), '--ack-active-target']).server_payload()
    finally:
        text_path.unlink(missing_ok=True)
    insert_ops = [step.get('op') for step in insert_payload['steps']]
    if insert_ops != ['where', 'set_text_file', 'where']:
        raise SystemExit(f'unexpected insert-text-file ops: {insert_ops!r}')
    insert_step = insert_payload['steps'][1]
    if insert_step.get('format') != 'UNICODE' or insert_step.get('option') != 'insertfile':
        raise SystemExit(f'unexpected set_text_file options: {insert_step!r}')

    expected_anchor_positions = {
        'insert-before-anchor': 'before-anchor',
        'insert-after-anchor': 'after-anchor',
        'insert-after-paragraph': 'after-paragraph',
        'insert-before-heading': 'before-heading',
    }
    for recipe_name, expected_position in expected_anchor_positions.items():
        anchor_payload = build_named_bundle(recipe_name, ['--target', '3.2 Test conditions', '--text', 'ordered text']).server_payload()
        anchor_ops = [step.get('op') for step in anchor_payload['steps']]
        if anchor_ops != ['anchor_insert', 'where']:
            raise SystemExit(f'unexpected {recipe_name} ops: {anchor_ops!r}')
        anchor_step = anchor_payload['steps'][0]
        if anchor_step.get('position') != expected_position or anchor_step.get('target') != '3.2 Test conditions' or anchor_step.get('text') != 'ordered text':
            raise SystemExit(f'unexpected {recipe_name} anchor_insert payload: {anchor_step!r}')
        if any(key in anchor_step for key in ('where', 'how', 'changed', 'summary', 'server_payload')):
            raise SystemExit(f'local metadata leaked into {recipe_name} server step: {anchor_step!r}')

    inventory_payload = build_named_bundle('section-control-inventory', ['--page-from', '19', '--page-to', '21']).server_payload()
    inventory_step = inventory_payload['steps'][0]
    if inventory_step.get('op') != 'control_inventory' or inventory_step.get('page_from') != 19 or inventory_step.get('page_to') != 21:
        raise SystemExit(f'unexpected section-control-inventory payload: {inventory_payload!r}')
    if any(key in inventory_step for key in ('where', 'how', 'changed')):
        raise SystemExit(f'local metadata leaked into inventory server step: {inventory_step!r}')

    export_payload = build_named_bundle('export-proof-range', ['--pages', '19-21', '--dpi', '160', '--out-dir', 'proof']).server_payload()
    if export_payload['steps'] != [{'op': 'export_pdf', 'label': 'export:fresh-pdf'}]:
        raise SystemExit(f'unexpected export-proof-range payload: {export_payload!r}')



def run_cli(argv: list[str]) -> int:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        return cli_main(argv)


def require_create_bundle_cli() -> None:
    with tempfile.TemporaryDirectory(prefix='hwpx-create-bundle-smoke-') as tmp_raw:
        tmp = Path(tmp_raw)
        text_path = tmp / 'body.txt'
        text_path.write_text('bundle smoke text', encoding='utf-8')

        strict_path = tmp / 'where.json'
        rc = run_cli(['create-bundle', str(strict_path), '--recipe', 'where'])
        if rc != 0:
            raise SystemExit('create-bundle where returned non-zero')
        strict_payload = json.loads(strict_path.read_text(encoding='utf-8'))
        if set(strict_payload) != {'steps'}:
            raise SystemExit(f'create-bundle strict output leaked non-step keys: {strict_payload!r}')
        if strict_payload['steps'] != [{'op': 'where', 'label': 'where:current-location'}]:
            raise SystemExit(f'unexpected create-bundle where JSON: {strict_payload!r}')

        meta_path = tmp / 'meta.json'
        rc = run_cli(['create-bundle', str(meta_path), '--with-meta', '--recipe', 'selected-text-proof'])
        if rc != 0:
            raise SystemExit('create-bundle --with-meta returned non-zero')
        meta_payload = json.loads(meta_path.read_text(encoding='utf-8'))
        if 'server_payload' not in meta_payload or 'sources' not in meta_payload:
            raise SystemExit(f'with-meta output missing metadata/server_payload: {meta_payload!r}')
        loaded = _load_bundle_payload(str(meta_path))
        if set(loaded) != {'steps', 'session_id'}:
            raise SystemExit(f'metadata wrapper did not load as strict request payload: {loaded!r}')
        if [step.get('op') for step in loaded['steps']] != ['where', 'get_selected_text', 'where']:
            raise SystemExit(f'with-meta server payload ops are wrong: {loaded!r}')

        raw_path = tmp / 'raw.json'
        rc = run_cli([
            'create-bundle',
            str(raw_path),
            '--step',
            f'set_text_file;path={text_path};ack_active_target=true',
        ])
        if rc != 0:
            raise SystemExit('create-bundle raw set_text_file returned non-zero')
        raw_payload = json.loads(raw_path.read_text(encoding='utf-8'))
        raw_step = raw_payload['steps'][0]
        if raw_step.get('op') != 'set_text_file' or raw_step.get('text') != 'bundle smoke text':
            raise SystemExit(f'raw set_text_file step did not serialize expected text: {raw_step!r}')

        if run_cli(['create-bundle', str(tmp / 'bad.json'), '--step', 'delete']) == 0:
            raise SystemExit('create-bundle must reject unsafe raw delete op')
        if run_cli(['create-bundle', str(tmp / 'bad2.json'), '--step', f'set_text_file;path={text_path}']) == 0:
            raise SystemExit('create-bundle raw set_text_file must require ack_active_target=true')

        composed = build_created_bundle(['where'], ['get_selected_text;keep_select=false'])
        if [step.get('op') for step in composed.server_payload()['steps']] != ['where', 'get_selected_text']:
            raise SystemExit('build_created_bundle did not compose recipe and raw step')

        section_path = tmp / 'section.json'
        rc = run_cli(['create-bundle', str(section_path), '--recipe', 'section-control-inventory --page-from 1 --page-to 2'])
        if rc != 0:
            raise SystemExit('create-bundle section-control-inventory returned non-zero')
        section_payload = json.loads(section_path.read_text(encoding='utf-8'))
        if section_payload['steps'][0].get('op') != 'control_inventory':
            raise SystemExit(f'section-control-inventory create-bundle used wrong op: {section_payload!r}')

        state_path = tmp / 'state.json'
        old_state_path = os.environ.get('HWPX_LOCAL_STATE_PATH')
        os.environ['HWPX_LOCAL_STATE_PATH'] = str(state_path)
        missing_session_tx_path = tmp / 'missing-session.tx.json'
        if run_cli(['tx-preview', str(missing_session_tx_path), '--recipe', 'paragraph-style-apply-exact', '--match', 'style target', '--expected-page', '3', '--keep-with-next', 'on', '--confirm-layout']) == 0:
            raise SystemExit('tx-preview must reject missing cached session_id')
        missing_session_plan_path = tmp / 'missing-session-plan.json'
        missing_session_plan_path.write_text(json.dumps({'server_payload': {'steps': [{'op': 'where', 'label': 'where:current-location'}], 'session_id': None}}), encoding='utf-8')
        try:
            _load_tx_server_payload(missing_session_plan_path)
        except Exception:
            pass
        else:
            raise SystemExit('tx-commit loader must reject null/missing session_id')

        state_path.write_text(json.dumps({'session_id': 'smoke-session'}), encoding='utf-8')
        tx_path = tmp / 'paragraph-style.tx.json'
        rc = run_cli([
            'tx-preview',
            str(tx_path),
            '--recipe',
            'paragraph-style-apply-exact',
            '--match',
            'style target',
            '--expected-page',
            '3',
            '--keep-with-next',
            'on',
            '--confirm-layout',
            '--with-meta',
        ])
        if rc != 0:
            raise SystemExit('tx-preview paragraph-style-apply-exact returned non-zero')
        tx_payload = json.loads(tx_path.read_text(encoding='utf-8'))
        if tx_payload.get('schema_version') != 'local-cli/tx-preview/v1' or tx_payload.get('auto_save') is not False:
            raise SystemExit(f'tx-preview missing transaction metadata: {tx_payload!r}')
        tx_server_payload = tx_payload.get('server_payload')
        if not isinstance(tx_server_payload, dict) or [step.get('op') for step in tx_server_payload.get('steps', [])] != ['where', 'paragraph_style_apply_exact', 'where']:
            raise SystemExit(f'tx-preview did not write strict paragraph style server_payload: {tx_payload!r}')
        loaded_tx_payload = _load_tx_server_payload(tx_path)
        if loaded_tx_payload != tx_server_payload:
            raise SystemExit('tx-commit loader must return the preview server_payload unchanged')
        if loaded_tx_payload.get('session_id') != 'smoke-session':
            raise SystemExit(f'tx-preview must preserve non-empty cached session_id: {loaded_tx_payload!r}')
        if old_state_path is None:
            os.environ.pop('HWPX_LOCAL_STATE_PATH', None)
        else:
            os.environ['HWPX_LOCAL_STATE_PATH'] = old_state_path

        fill_spec = build_named_bundle(
            'section-frame-fill',
            ['--page-from', '1', '--target-id', 'ctrl/1/gso/no-inst', '--text-file', str(text_path), '--style-source', 'style-anchor'],
        ).debug_payload()
        if fill_spec.get('sources', [{}])[0].get('type') != 'guarded-mutation-spec':
            raise SystemExit(f'guarded frame fill spec did not include blocker metadata: {fill_spec!r}')
        if fill_spec['server_payload']['steps'][0].get('op') != 'control_inventory':
            raise SystemExit(f'guarded frame fill must only emit inventory proof: {fill_spec!r}')

        remove_spec = build_named_bundle(
            'section-graphic-remove-or-hide',
            ['--page-from', '1', '--target-id', 'ctrl/1/gso/no-inst', '--expected-hash', 'sha256:fixture', '--hide-only'],
        ).debug_payload()
        if remove_spec['server_payload']['steps'][0].get('op') != 'control_inventory':
            raise SystemExit(f'guarded remove/hide must only emit inventory proof: {remove_spec!r}')

        delete_spec = build_named_bundle(
            'section-control-delete-exact',
            [
                '--page-from', '19',
                '--page-to', '19',
                '--target-id', 'ctrl/93/gso/no-inst',
                '--expected-hash', 'sha256:fixture',
                '--expected-page', '19',
                '--confirm-remove',
            ],
        ).debug_payload()
        delete_steps = delete_spec['server_payload']['steps']
        delete_ops = [step.get('op') for step in delete_steps]
        if delete_ops != ['where', 'control_delete_exact', 'where']:
            raise SystemExit(f'exact delete must wrap one mutation with before/after where proof: {delete_spec!r}')
        delete_step = delete_steps[1]
        if not delete_step.get('confirm_remove') or delete_step.get('expected_page') != 19:
            raise SystemExit(f'exact delete did not preserve confirmation/page guard: {delete_step!r}')
        if delete_step.get('target_id') != 'ctrl/93/gso/no-inst' or delete_step.get('expected_hash') != 'sha256:fixture':
            raise SystemExit(f'exact delete did not preserve target/hash guard: {delete_step!r}')

        select_spec = build_named_bundle(
            'exact-control-select-proof',
            [
                '--page-from', '19',
                '--target-id', 'ctrl/93/gso/1234',
                '--expected-hash', 'sha256:fixture',
                '--expected-page', '19',
            ],
        ).debug_payload()
        select_ops = [step.get('op') for step in select_spec['server_payload']['steps']]
        if select_ops != ['where', 'exact_control_select_proof', 'where']:
            raise SystemExit(f'exact control select proof must wrap one proof step with before/after where: {select_spec!r}')

        cell_format_spec = build_named_bundle(
            'cell-format-exact',
            [
                '--page-from', '19',
                '--target-id', 'ctrl/94/tbl/1235',
                '--expected-hash', 'sha256:fixture',
                '--expected-page', '19',
                '--cell-margin-mm', '1.5',
                '--confirm-layout',
            ],
        ).debug_payload()
        cell_format_step = cell_format_spec['server_payload']['steps'][1]
        if cell_format_step.get('op') != 'cell_format_exact' or cell_format_step.get('cell_margin_mm') != 1.5:
            raise SystemExit(f'cell-format-exact did not preserve target format request: {cell_format_spec!r}')

        paragraph_style_spec = build_named_bundle(
            'paragraph-style-apply-exact',
            [
                '--match', 'style target',
                '--expected-page', '3',
                '--keep-with-next', 'on',
                '--widow-orphan', 'off',
                '--pagebreak-before', '1',
                '--confirm-layout',
            ],
        ).debug_payload()
        paragraph_style_steps = paragraph_style_spec['server_payload']['steps']
        if [step.get('op') for step in paragraph_style_steps] != ['where', 'paragraph_style_apply_exact', 'where']:
            raise SystemExit(f'paragraph-style-apply-exact must wrap one mutation with before/after where: {paragraph_style_spec!r}')
        paragraph_style_step = paragraph_style_steps[1]
        if paragraph_style_step.get('expected_page') != 3 or paragraph_style_step.get('keep_with_next') is not True or paragraph_style_step.get('widow_orphan') is not False or paragraph_style_step.get('pagebreak_before') != 1:
            raise SystemExit(f'paragraph-style-apply-exact did not preserve style/page fields: {paragraph_style_step!r}')

        structure_spec = build_named_bundle(
            'table-cell-structure-exact',
            [
                '--page-from', '25',
                '--target-id', 'ctrl/99/tbl/2038321113',
                '--expected-hash', 'sha256:f56d0b58bc9520222cd6fabd',
                '--expected-page', '25',
            ],
        ).debug_payload()
        structure_steps = structure_spec['server_payload']['steps']
        if [step.get('op') for step in structure_steps] != ['where', 'table_cell_structure_exact', 'where']:
            raise SystemExit(f'table-cell-structure-exact must wrap one read-only proof step with before/after where: {structure_spec!r}')
        structure_step = structure_steps[1]
        if structure_step.get('target_id') != 'ctrl/99/tbl/2038321113' or structure_step.get('expected_page') != 25:
            raise SystemExit(f'table-cell-structure-exact did not preserve target/page guard: {structure_step!r}')

def require_unsafe_rejection() -> None:
    for name in ('delete', 'erase', 'table-delete', 'cell-clear-contents'):
        try:
            build_named_bundle(name)
        except BundleError:
            continue
        raise SystemExit(f'unsafe first-pass recipe was not rejected: {name}')

    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as handle:
        handle.write('bundle smoke text')
        text_path = Path(handle.name)
    try:
        try:
            build_named_bundle('insert-text-file', [str(text_path)])
        except BundleError:
            pass
        else:
            raise SystemExit('insert-text-file must require --ack-active-target')
    finally:
        text_path.unlink(missing_ok=True)


def main() -> int:
    require_contains(ROOT / 'app' / 'local_cli_router.py', "@router.post('/local-cli/command-bundle')")
    require_contains(ROOT / 'app' / 'local_cli_service.py', 'def command_bundle(')
    require_contains(ROOT / 'app' / 'local_cli_service.py', '_BUNDLE_SAFE_HACTION_NAMES')
    require_not_contains(ROOT / 'app' / 'local_cli_service.py', "'move_anchor'")
    require_not_contains(ROOT / 'app' / 'local_cli_service.py', "'cell_proof'")
    require_not_contains(ROOT / 'app' / 'local_cli_service.py', "'cell_clear_contents'")
    require_contains(ROOT / 'local_cli_v1' / 'main.py', "bundle_dump_parser = subparsers.add_parser")
    require_contains(ROOT / 'local_cli_v1' / 'main.py', "'create-bundle'")
    require_contains(ROOT / 'local_cli_v1' / 'main.py', 'normalize_command_bundle')
    require_contains(ROOT / 'local_cli_v1' / 'bundles.py', 'class BundleSpec')
    require_contains(ROOT / 'local_cli_v1' / 'output_parser.py', 'def normalize_command_bundle')
    require_contains(ROOT / 'local_cli_v1' / 'output_parser.py', 'def format_where_from_bundle')
    require_contains(ROOT / 'local_cli_v1' / 'output_parser.py', 'def format_where_bundle_human')
    require_contains(ROOT / 'local_cli_v1' / 'README.md', 'Standard safe edit+proof workflow')
    require_contains(ROOT / 'local_cli_v1' / 'README.md', 'bundle-backed / direct-backlog / disabled')
    require_contains(ROOT / 'local_cli_v1' / 'README.md', 'Uniform command pipeline and migration backlog')
    require_contains(ROOT / 'local_cli_v1' / 'README.md', '`where`: migrated to the bundle-backed public command path')
    require_contains(ROOT / 'local_cli_v1' / 'README.md', 'not migrated yet')
    require_contains(ROOT / 'local_cli_v1' / 'README.md', 'Bundle output parsing and user-facing formatting now live locally')
    require_contains(ROOT / 'local_cli_v1' / 'main.py', "_execute_named_bundle(base_url, 'where')")
    require_contains(ROOT / 'local_cli_v1' / 'main.py', 'temporary legacy /local-cli/where fallback')
    require_contains(ROOT / 'local_cli_v1' / 'main.py', "'section-control-inventory'")
    require_contains(ROOT / 'local_cli_v1' / 'main.py', "'export-proof-range'")
    require_contains(ROOT / 'local_cli_v1' / 'main.py', "'command-status'")
    require_contains(ROOT / 'local_cli_v1' / 'main.py', 'SAFE_WORKFLOW_TEXT')
    require_contains(ROOT / 'local_cli_v1' / 'output_parser.py', 'def summarize_section_control_inventory')
    require_contains(ROOT / 'local_cli_v1' / 'output_parser.py', 'def summarize_table_cell_structure')
    require_contains(ROOT / 'app' / 'local_cli_service.py', "'control_inventory'")
    require_contains(ROOT / 'app' / 'local_cli_service.py', "'export_pdf'")
    require_bundle_shape()
    require_bundle_op_drift_check()
    require_create_bundle_cli()
    require_unsafe_rejection()
    print('command-bundle static smoke: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
