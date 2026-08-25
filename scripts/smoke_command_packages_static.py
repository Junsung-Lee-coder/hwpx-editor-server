from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.command_packages.runtime import get_command_package_registry  # noqa: E402
from app.command_packages.commands.context.run import _current_paragraph_probe, _current_visual_line_probe  # noqa: E402
from local_cli_v1.bundles import _STEP_KEYS, build_named_bundle  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


class _ValidationError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class _ValidationService:
    def _validate_action_name(self, action_name: str) -> str:
        action = str(action_name or '').strip()
        if not action:
            raise _ValidationError('action_name must not be empty', status_code=400)
        return action

    def _validate_macro_path(self, method_path: str) -> tuple[str, list[str]]:
        path = str(method_path or '').strip()
        if not path:
            raise _ValidationError('pycall path must not be empty', status_code=400)
        return path, path.split('.')

    def _validate_macro_args(self, args: object, kwargs: object) -> tuple[list[object], dict[str, object]]:
        return list(args or []), dict(kwargs or {})

    def _normalize_anchor_insert_position(self, value: object) -> str:
        raw = str(value or 'before-anchor').strip().lower().replace('_', '-').replace(' ', '-')
        if raw in {'before', 'before-anchor'}:
            return 'before-anchor'
        if raw in {'after', 'after-anchor'}:
            return 'after-anchor'
        if raw in {'after-paragraph', 'paragraph-end'}:
            return 'after-paragraph'
        if raw in {'before-heading'}:
            return 'before-heading'
        raise _ValidationError(f'unsupported anchor insert position: {value!r}', status_code=400)


_VALIDATION_SAMPLES: dict[str, dict[str, object]] = {
    'context': {},
    'selection_proof': {},
    'control_inventory': {'section_anchor': 'A'},
    'table_frame_inventory': {'page_from': 1},
    'export_pdf': {},
    'hwp_action': {'action_name': 'MoveDocBegin'},
    'pyhwpx_call': {'method_path': 'get_pos', 'args': [], 'kwargs': {}},
    'save_document': {},
    'set_text_file': {'text': 'hello'},
    'anchor_insert': {'target': 'anchor', 'position': 'after-anchor', 'text': 'inserted'},
    'get_selected_text': {'keep_select': False},
    'readback': {'scope': 'caret', 'max_blocks': 3, 'max_table_cells': 3, 'max_controls': 3},
    'typography_overview': {'scope': 'document', 'max_samples': 3, 'max_sections': 3, 'max_styles': 3},
    'style_inspect': {'match': 'hello', 'keep_position': True},
    'paragraph_style_apply_exact': {'match': 'hello', 'expected_page': 1, 'confirm_layout': True},
    'paragraph_delete_exact': {'match': 'hello', 'expected_page': 1, 'confirm_remove': True},
    'paragraph_join_previous_exact': {'match': 'hello', 'expected_page': 1, 'confirm_layout': True},
    'paragraph_join_next_exact': {'match': 'hello', 'expected_page': 1, 'confirm_layout': True},
    'control_join_previous_exact': {'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1, 'confirm_layout': True},
    'paragraph_rehome_exact': {
        'delete_match': 'a',
        'delete_expected_page': 1,
        'insert_before_match': 'b',
        'insert_expected_page': 1,
        'insert_text': 'a',
        'expected_text_delta': 0,
        'confirm_layout': True,
    },
    'control_delete_exact': {'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1, 'confirm_remove': True},
    'exact_control_select_proof': {'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1},
    'control_move_resize_exact': {'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1, 'scale_percent': 100, 'confirm_layout': True},
    'cell_format_exact': {'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1, 'vertical_align': 'middle', 'confirm_layout': True},
    'cell_row_fit_exact': {'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1, 'row_height_percent': 80, 'confirm_layout': True},
    'native_table_insert': {
        'rows': 2,
        'cols': 2,
        'cells': [['A', 'B'], ['1 kg', '2.5 mm']],
        'field_name': '__rumi_native_table_cells__',
        'confirm_native_table': True,
        'source_text_deleted': False,
        'old_plain_text_removal': 'deferred_until_rendered_proof',
        'flat_value_hashes': [
            'sha256:559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd',
            'sha256:df7e70e5021544f4834bbee64a9e3789febc4be81470df629cad6ddb03320a5c',
            'sha256:dbf6c4777afdd2fa7dbd544fb43c9d939989659c5e606e659e24a17ec9a1c8e4',
            'sha256:2a390fd2676e71524562eebb5c367ebf4cf21515bc84eabbd7c2befd2dabe959',
        ],
        'non_empty_token_count': 4,
        'non_empty_token_preview': ['A', 'B', '1 kg', '2.5 mm'],
    },
    'anchor_range_replace_native_table': {
        'section_anchor': 'Section',
        'start_anchor': 'Start',
        'end_before_anchor': 'End',
        'rows': 1,
        'cols': 4,
        'cells': [['A', 'B', 'C', 'D']],
        'field_name': '__rumi_anchor_range_table__',
        'confirm_replace': True,
        'flat_value_hashes': [
            'sha256:559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd',
            'sha256:df7e70e5021544f4834bbee64a9e3789febc4be81470df629cad6ddb03320a5c',
            'sha256:6b23c0d5f35d1b11f9b683f0b0a617355deb11277d91ae091d399c655b87940d',
            'sha256:3f39d5c348e5b79d06e842c114e6cc571583bbf44e4b0ebfda1a01ec05745d43',
        ],
        'non_empty_token_count': 4,
        'non_empty_token_preview': ['A', 'B', 'C', 'D'],
        'next_proof_required': 'rendered proof required',
    },
    'selected_text_delete_exact': {
        'expected_text': 'old pipe text',
        'expected_hash': 'sha256:' + __import__('hashlib').sha256('old pipe text'.encode('utf-8')).hexdigest(),
        'expected_normalized_hash': 'sha256:' + __import__('hashlib').sha256('old pipe text'.encode('utf-8')).hexdigest(),
        'confirm_cleanup': True,
        'source_text_deleted': False,
    },
    'table_cell_structure_exact': {'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1},
    'table_column_width_exact': {
        'page_from': 1,
        'target_id': 'ctrl/1',
        'expected_hash': 'sha256:abc',
        'expected_page': 1,
        'expected_preimage_sha256': 'sha256:preimage',
        'expected_cell_inventory_hash': 'sha256:cells',
        'expected_document_text_hash': 'sha256:text',
        'expected_text_char_count': 10,
        'expected_nonempty_line_count': 2,
        'expected_div0_count': 0,
        'expected_rows': 2,
        'expected_cols': 2,
        'expected_total_width_mm': 60.0,
        'expected_table_height_mm': 20.0,
        'expected_control_count': 1,
        'expected_bindata_manifest_hash': 'sha256:bindata',
        'requested_widths_mm': [30.0, 30.0],
        'confirm_layout': True,
    },
    'table_split_exact': {'page_from': 1, 'target_id': 'ctrl/1', 'expected_hash': 'sha256:abc', 'expected_page': 1, 'down_rows': 1, 'confirm_layout': True},
    'where': {},
}


class _FakeContextHwp:
    def __init__(self) -> None:
        self.pos = [0, 2, 3]
        self.selected_pos: tuple[object, ...] = (False, 0, 0, 0, 0, 0, 0)
        self.paragraphs = {2: 'Alpha beta gamma'}
        self.actions: list[str] = []

    def get_pos(self) -> tuple[int, int, int]:
        return tuple(self.pos)

    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        self.pos = [int(list_id), int(para), int(pos)]
        self.selected_pos = (False, 0, 0, 0, 0, 0, 0)
        return True

    def get_selected_pos(self) -> tuple[object, ...]:
        return self.selected_pos

    def select_text(self, *args: object) -> bool:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            self.selected_pos = tuple(args[0])
            return True
        if len(args) != 5:
            raise TypeError(f'unexpected select_text args: {args!r}')
        spara, spos, epara, epos, slist = (int(item) for item in args)
        self.selected_pos = (True, slist, spara, spos, slist, epara, epos)
        return True

    def get_selected_text(self, *, keep_select: bool = True) -> str:
        selected = self.selected_pos
        if not selected or selected[0] is not True:
            return ''
        _is_block, _slist, spara, spos, _elist, epara, epos = selected
        if int(spara) != int(epara):
            return ''
        text = self.paragraphs.get(int(spara), '')
        end = len(text) if int(epos) < 0 else int(epos)
        return text[int(spos):end]

    def Run(self, action_name: str) -> bool:
        self.actions.append(action_name)
        if action_name == 'MoveLineBegin':
            self.set_pos(self.pos[0], self.pos[1], 0)
            return True
        if action_name == 'MoveSelLineEnd':
            self.selected_pos = (True, self.pos[0], self.pos[1], self.pos[2], self.pos[0], self.pos[1], 5)
            return True
        return False


def require_context_probe_helpers() -> None:
    hwp = _FakeContextHwp()
    paragraph = _current_paragraph_probe(hwp)
    require(paragraph.get('available') is True, f'paragraph probe failed: {paragraph!r}')
    require(paragraph.get('text') == 'Alpha beta gamma', f'paragraph probe selected wrong text: {paragraph!r}')
    require(paragraph.get('restore', {}).get('restored') is True, f'paragraph probe did not restore: {paragraph!r}')
    require(hwp.get_pos() == (0, 2, 3), f'paragraph probe left position changed: {hwp.get_pos()!r}')

    line = _current_visual_line_probe(hwp)
    require(line.get('available') is True, f'line probe failed: {line!r}')
    require(line.get('text') == 'Alpha', f'line probe selected wrong text: {line!r}')
    require([item.get('action') for item in line.get('actions', [])] == ['set_pos(saved_pos)', 'MoveLineBegin', 'MoveSelLineEnd'], f'line probe action drift: {line!r}')
    require(line.get('restore', {}).get('restored') is True, f'line probe did not restore: {line!r}')
    require(hwp.get_pos() == (0, 2, 3), f'line probe left position changed: {hwp.get_pos()!r}')


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


def _literal_allowed_keys(path: Path) -> dict[str, set[str]]:
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
        return {key: set(items) for key, items in value.items()}
    raise SystemExit('allowed_keys assignment not found in app/local_cli_service.py')


def main() -> int:
    registry = get_command_package_registry()
    ops = registry.ops()
    server_path = ROOT / 'app' / 'local_cli_service.py'
    server_ops = _literal_string_set_assignment(server_path, '_BUNDLE_ALLOWED_OPS')
    server_allowed_keys = _literal_allowed_keys(server_path)

    require(ops == server_ops, f'command package coverage drift: missing={sorted(server_ops - ops)} extra={sorted(ops - server_ops)}')
    require(set(server_allowed_keys) == server_ops, f'server allowed-key ops drift: missing={sorted(server_ops - set(server_allowed_keys))}')

    for op in sorted(server_ops):
        package = registry.get(op)
        require(package is not None, f'{op} package missing')
        require(package.allowed_keys == server_allowed_keys[op], f'{op} allowed keys drifted: package={sorted(package.allowed_keys)} server={sorted(server_allowed_keys[op])}')
        require(callable(getattr(package.module, 'validate_step', None)), f'{op} package missing validate_step')
        require(callable(getattr(package.module, 'run_step', None)), f'{op} package missing run_step')
        if op in _STEP_KEYS:
            require(set(_STEP_KEYS[op]) <= package.allowed_keys, f'{op} package must accept local bundle keys')
            require({'op', 'operation', 'label'} <= package.allowed_keys, f'{op} package must keep explicit JSON command-bundle metadata keys')

        sample = {'op': op, 'label': f'test:{op}', **_VALIDATION_SAMPLES[op]}
        validated = package.validate(service=_ValidationService(), index=1, step=sample, error_type=_ValidationError)
        require(validated.get('op') == op, f'{op} validate_step must preserve op')

    where = registry.get('where')
    require(where is not None, 'where package missing')
    require(where.read_only is True, 'where package must be read-only')
    require(where.allowed_keys == {'op', 'operation', 'label'}, f'unexpected where allowed keys: {where.allowed_keys!r}')
    require(build_named_bundle('where').server_payload()['steps'] == [{'op': 'where', 'label': 'where:current-location'}], 'where bundle payload drifted')

    context = registry.get('context')
    require(context is not None, 'context package missing')
    require(context.read_only is True, 'context package must be read-only')
    require(context.allowed_keys == {'op', 'operation', 'label'}, f'unexpected context allowed keys: {context.allowed_keys!r}')
    require(build_named_bundle('context').server_payload()['steps'] == [{'op': 'context', 'label': 'context:edit-position'}], 'context bundle payload drifted')
    context_source = (ROOT / 'app' / 'command_packages' / 'commands' / 'context' / 'run.py').read_text(encoding='utf-8')
    require("'paragraph_context': paragraph_context" in context_source, 'context package must expose paragraph_context')
    require("'line_context': line_context" in context_source, 'context package must expose line_context')
    require("'selection_text_probes': text_probes" in context_source, 'context package must expose selection_text_probes')
    require_context_probe_helpers()

    style = registry.get('style_inspect')
    require(style is not None, 'style_inspect package missing')
    require(style.read_only is True, 'style_inspect package must be read-only')

    revision = registry.revision()
    require('context/manifest.json' in revision and 'where/manifest.json' in revision and 'style_inspect/run.py' in revision and 'typography_overview/run.py' in revision and 'set_text_file/run.py' in revision, f'package revision missing expected files: {revision!r}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
