from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


BUNDLE_SERVER_OPS = frozenset(
    {
        'context',
        'selection_proof',
        'control_inventory',
        'table_frame_inventory',
        'export_pdf',
        'hwp_action',
        'pyhwpx_call',
        'set_text_file',
        'anchor_insert',
        'get_selected_text',
        'readback',
        'typography_overview',
        'style_inspect',
        'paragraph_style_apply_exact',
        'paragraph_delete_exact',
        'control_delete_exact',
        'exact_control_select_proof',
        'control_move_resize_exact',
        'cell_format_exact',
        'cell_row_fit_exact',
        'native_table_insert',
        'anchor_range_replace_native_table',
        'selected_text_delete_exact',
        'table_cell_structure_exact',
        'table_column_width_exact',
        'table_split_exact',
        'where',
    }
)

_BLOCKED_BUNDLE_NAMES: dict[str, str] = {
    'delete': 'not migrated yet: delete/erase needs explicit target semantics, modal handling, and rendered proof.',
    'erase': 'not migrated yet: delete/erase needs explicit target semantics, modal handling, and rendered proof.',
    'table-delete': 'not migrated yet: table-delete needs modal-safe semantics, disposable-document proof, and rendered proof.',
    'cell-clear-contents': 'not migrated yet: cell clearing needs low-risk table-structure proof and rendered proof.',
}

_STEP_KEYS: dict[str, frozenset[str]] = {
    'context': frozenset({'op', 'label'}),
    'selection_proof': frozenset({'op', 'label'}),
    'control_inventory': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'max_controls',
        }
    ),
    'table_frame_inventory': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'max_controls',
        }
    ),
    'export_pdf': frozenset({'op', 'label'}),
    'hwp_action': frozenset({'op', 'label', 'action_name'}),
    'pyhwpx_call': frozenset({'op', 'label', 'method_path', 'args', 'kwargs'}),
    'set_text_file': frozenset({'op', 'label', 'text', 'format', 'option'}),
    'anchor_insert': frozenset({'op', 'label', 'target', 'position', 'text', 'fragments'}),
    'get_selected_text': frozenset({'op', 'label', 'keep_select'}),
    'readback': frozenset({'op', 'label', 'scope', 'page_from', 'page_to', 'max_blocks', 'max_table_cells', 'max_controls'}),
    'typography_overview': frozenset({'op', 'label', 'scope', 'max_samples', 'max_sections', 'max_styles'}),
    'style_inspect': frozenset({'op', 'label', 'match', 'keep_position'}),
    'paragraph_style_apply_exact': frozenset(
        {
            'op',
            'operation',
            'label',
            'match',
            'expected_page',
            'keep_with_next',
            'widow_orphan',
            'pagebreak_before',
            'confirm_layout',
        }
    ),
    'paragraph_delete_exact': frozenset(
        {
            'op',
            'operation',
            'label',
            'match',
            'expected_page',
            'occurrence_on_page',
            'expected_previous_contains',
            'expected_next_contains',
            'confirm_remove',
            'max_page_after',
        }
    ),
    'control_delete_exact': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'confirm_remove',
            'max_controls',
        }
    ),
    'exact_control_select_proof': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'max_controls',
        }
    ),
    'cell_format_exact': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'cell_margin_hu',
            'cell_margin_mm',
            'vertical_align',
            'fill_color',
            'border',
            'confirm_layout',
            'max_controls',
        }
    ),
    'cell_row_fit_exact': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'row_height_percent',
            'row_height_hu',
            'row_height_mm',
            'resize_up_steps',
            'resize_down_steps',
            'line_spacing',
            'char_height_percent',
            'confirm_layout',
            'max_controls',
        }
    ),
    'native_table_insert': frozenset(
        {
            'op',
            'operation',
            'label',
            'rows',
            'cols',
            'cells',
            'field_name',
            'confirm_native_table',
            'source_text_deleted',
            'old_plain_text_removal',
            'flat_value_hashes',
            'non_empty_token_count',
            'non_empty_token_preview',
            'warnings',
            'next_proof_required',
        }
    ),

    'anchor_range_replace_native_table': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'start_anchor',
            'end_before_anchor',
            'required_source_basename',
            'forbid_source_basename',
            'expected_range_hash',
            'expected_normalized_range_hash',
            'caption_text',
            'rows',
            'cols',
            'cells',
            'field_name',
            'confirm_replace',
            'flat_value_hashes',
            'non_empty_token_count',
            'non_empty_token_preview',
            'warnings',
            'next_proof_required',
        }
    ),

    'selected_text_delete_exact': frozenset(
        {
            'op',
            'label',
            'expected_text',
            'expected_hash',
            'expected_normalized_hash',
            'confirm_cleanup',
            'source_text_deleted',
            'native_table_proof_ref',
            'native_table_proof_hash',
        }
    ),
    'table_cell_structure_exact': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'max_controls',
        }
    ),
    'table_column_width_exact': frozenset(
        {
            'op',
            'label',
            'operation',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'expected_preimage_sha256',
            'expected_cell_inventory_hash',
            'expected_document_text_hash',
            'expected_text_char_count',
            'expected_nonempty_line_count',
            'expected_div0_count',
            'expected_rows',
            'expected_cols',
            'expected_total_width_mm',
            'expected_table_height_mm',
            'expected_control_count',
            'expected_bindata_manifest_hash',
            'requested_widths_mm',
            'confirm_layout',
            'max_controls',
        }
    ),
    'table_split_exact': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'down_rows',
            'confirm_layout',
            'max_controls',
        }
    ),
    'control_move_resize_exact': frozenset(
        {
            'op',
            'label',
            'section_anchor',
            'page_from',
            'page_to',
            'around',
            'target_id',
            'expected_hash',
            'expected_page',
            'scale_percent',
            'move_dx_mm',
            'move_dy_mm',
            'confirm_layout',
            'max_controls',
        }
    ),
    'where': frozenset({'op', 'label'}),
}


class BundleError(ValueError):
    """Raised when a local bundle recipe cannot be built safely."""


@dataclass(frozen=True)
class CommandStep:
    """A strict server command-bundle step.

    This object validates the JSON shape sent to `/local-cli/command-bundle`.
    Local user-facing metadata stays on BundleSpec and is never embedded in the
    server step payload.
    """

    op: str
    label: str
    fields: dict[str, Any]

    def __post_init__(self) -> None:
        op = self.op.strip()
        if op not in BUNDLE_SERVER_OPS:
            raise BundleError(f'Unsupported bundle op: {self.op!r}')
        label = self.label.strip()
        if not label or len(label) > 80:
            raise BundleError('Bundle step label must be 1-80 characters.')
        if not isinstance(self.fields, dict):
            raise BundleError('Bundle step fields must be an object.')
        keys = {'op', 'label', *self.fields.keys()}
        unsupported = sorted(keys - _STEP_KEYS[op])
        if unsupported:
            raise BundleError(f'Unsupported fields for op={op!r}: {", ".join(unsupported)}')
        object.__setattr__(self, 'op', op)
        object.__setattr__(self, 'label', label)

    def to_server_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {'op': self.op, 'label': self.label}
        payload.update(self.fields)
        return payload


@dataclass(frozen=True)
class BundleSpec:
    """A local planner bundle with user-visible proof metadata."""

    name: str
    summary: str
    where: str
    how: str
    changed: str
    steps: tuple[CommandStep, ...]
    sources: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise BundleError('Bundle name is required.')
        if not self.steps:
            raise BundleError('Bundle must contain at least one server step.')

    def server_payload(self) -> dict[str, Any]:
        """Return the strict JSON body for `/local-cli/command-bundle`."""
        return {'steps': [step.to_server_json() for step in self.steps]}

    def local_metadata(self) -> dict[str, str]:
        return {
            'name': self.name,
            'summary': self.summary,
            'where': self.where,
            'how': self.how,
            'changed': self.changed,
        }

    def debug_payload(self) -> dict[str, Any]:
        payload = self.local_metadata()
        if self.sources:
            payload['sources'] = [dict(source) for source in self.sources]
        payload['server_payload'] = self.server_payload()
        return payload


@dataclass(frozen=True)
class BundleRecipe:
    name: str
    summary: str
    build: Callable[[Sequence[str]], BundleSpec]


def _step(op: str, label: str, **fields: Any) -> CommandStep:
    return CommandStep(op=op, label=label, fields=fields)


def _where_step(label: str = 'where:current-location') -> CommandStep:
    return _step('where', label)


def _context_step(label: str = 'context:edit-position') -> CommandStep:
    return _step('context', label)


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f'hwpx bundle-dump {prog}', description=description)


def _coerce_bool(raw: str, *, field: str) -> bool:
    value = raw.strip().lower()
    if value in {'1', 'true', 'yes', 'y', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'n', 'off'}:
        return False
    raise BundleError(f'{field} must be true or false.')


def _positive_int(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise BundleError(f'{field} must be a positive integer.')
    return int(value)


def _bounded_text(value: str | None, *, field: str, max_chars: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise BundleError(f'{field} must not be empty when provided.')
    if len(text) > max_chars:
        raise BundleError(f'{field} is too long (max {max_chars} characters).')
    return text


_PIPE_SEPARATOR_RE = re.compile(r'^:?-{3,}:?$')


def _is_markdown_separator_row(cells: Sequence[str]) -> bool:
    return bool(cells) and all(_PIPE_SEPARATOR_RE.fullmatch(cell.strip()) for cell in cells)


def parse_pipe_table(text: str) -> list[list[str]]:
    lines = [line for line in str(text or '').splitlines() if line.strip()]
    if not lines:
        raise BundleError('text-table-to-native requires a non-empty pipe table.')

    rows: list[list[str]] = []
    expected_cols: int | None = None
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if '|' not in line:
            raise BundleError(f'table line {line_number} is not a pipe-delimited row.')
        if line.startswith('|'):
            line = line[1:]
        if line.endswith('|'):
            line = line[:-1]
        cells = [cell.strip() for cell in line.split('|')]
        if not cells:
            raise BundleError(f'table line {line_number} has no cells.')
        if _is_markdown_separator_row(cells):
            if rows and expected_cols == len(cells):
                continue
            raise BundleError('Markdown table separator row must follow a same-width header row.')
        if any('\n' in cell or '\r' in cell for cell in cells):
            raise BundleError(f'table line {line_number} contains an embedded line break in a cell.')
        if expected_cols is None:
            expected_cols = len(cells)
            if expected_cols <= 0:
                raise BundleError('text-table-to-native requires at least one column.')
        elif len(cells) != expected_cols:
            raise BundleError(
                f'non-rectangular table: line {line_number} has {len(cells)} cells; expected {expected_cols}.'
            )
        rows.append(cells)

    if not rows:
        raise BundleError('text-table-to-native requires at least one non-separator row.')
    if not any(cell.strip() for row in rows for cell in row):
        raise BundleError('text-table-to-native rejects all-empty table content.')
    return rows


def _hash_cell_value(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def _normalize_header_name(value: str) -> str:
    return ' '.join(str(value or '').split()).strip()


def _resolve_split_column(header: Sequence[str], requested: str) -> int:
    requested_name = _normalize_header_name(requested)
    if not requested_name:
        raise BundleError('--split-by-column must name a non-empty header cell.')
    normalized = [_normalize_header_name(cell) for cell in header]
    exact_matches = [index for index, cell in enumerate(normalized) if cell == requested_name]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise BundleError(f'--split-by-column {requested_name!r} is ambiguous in the header row.')
    folded_requested = requested_name.casefold()
    folded_matches = [index for index, cell in enumerate(normalized) if cell.casefold() == folded_requested]
    if len(folded_matches) == 1:
        return folded_matches[0]
    if len(folded_matches) > 1:
        raise BundleError(f'--split-by-column {requested_name!r} is ambiguous in the header row.')
    available = ', '.join(cell for cell in normalized if cell) or '(no non-empty headers)'
    raise BundleError(f'--split-by-column {requested_name!r} was not found in the header row. Available headers: {available}')


def _split_pipe_table_by_column(cells: Sequence[Sequence[str]], requested_header: str) -> tuple[int, tuple[tuple[str, list[list[str]]], ...]]:
    if len(cells) < 2:
        raise BundleError('text-table-to-native --split-by-column requires a header row plus at least one data row.')
    header = list(cells[0])
    column_index = _resolve_split_column(header, requested_header)
    grouped: dict[str, list[list[str]]] = {}
    for row_number, row in enumerate(cells[1:], start=2):
        group_value = str(row[column_index]).strip()
        if not group_value:
            raise BundleError(f'text-table-to-native --split-by-column found an empty group value at row {row_number}.')
        grouped.setdefault(group_value, [header.copy()]).append(list(row))
    if len(grouped) < 2:
        raise BundleError('text-table-to-native --split-by-column requires at least two distinct group values.')
    return column_index, tuple((group_value, rows) for group_value, rows in grouped.items())


def _field_name_for_split(base_field_name: str, split_index: int) -> str:
    suffix = f'_g{split_index:03d}'
    candidate = f'{base_field_name}{suffix}'
    if len(candidate) <= 80:
        return candidate
    digest = hashlib.sha256(candidate.encode('utf-8')).hexdigest()[:10]
    return f'{base_field_name[: max(1, 80 - len(suffix) - len(digest) - 1)]}_{digest}{suffix}'


def _read_table_text_from_args(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.from_file is None and args.text is None:
        raise BundleError('text-table-to-native requires --from-file or --text.')
    if args.from_file is not None and args.text is not None:
        raise BundleError('Use exactly one of --from-file or --text for a fail-closed source boundary.')
    if args.from_file is not None:
        path = Path(args.from_file).expanduser()
        if not path.exists() or not path.is_file():
            raise BundleError(f'table source file not found: {path}')
        text = path.read_text(encoding='utf-8')
        return text, {'type': 'from-file', 'path': str(path)}
    return str(args.text), {'type': 'inline-text'}



def _read_exact_text_from_args(args: argparse.Namespace, *, command_name: str) -> tuple[str, dict[str, Any]]:
    if args.from_file is None and args.text is None:
        raise BundleError(f'{command_name} requires --from-file or --text.')
    if args.from_file is not None and args.text is not None:
        raise BundleError('Use exactly one of --from-file or --text for a fail-closed source boundary.')
    if args.from_file is not None:
        path = Path(args.from_file).expanduser()
        if not path.exists() or not path.is_file():
            raise BundleError(f'source file not found: {path}')
        text = path.read_text(encoding='utf-8')
        return text, {'type': 'from-file', 'path': str(path)}
    return str(args.text), {'type': 'inline-text'}


def _normalize_visible_text(value: str | None) -> str:
    return ' '.join(str(value or '').split()).strip()


def _control_inventory_step(
    label: str,
    *,
    section_anchor: str | None = None,
    page_from: int | None = None,
    page_to: int | None = None,
    around: str | None = None,
    target_id: str | None = None,
    expected_hash: str | None = None,
    expected_page: int | None = None,
    max_controls: int = 2048,
) -> CommandStep:
    fields: dict[str, Any] = {'max_controls': max_controls}
    for key, value in (
        ('section_anchor', section_anchor),
        ('around', around),
        ('target_id', target_id),
        ('expected_hash', expected_hash),
    ):
        if value is not None:
            fields[key] = value
    for key, value in (('page_from', page_from), ('page_to', page_to), ('expected_page', expected_page)):
        if value is not None:
            fields[key] = value
    return _step('control_inventory', label, **fields)


def _table_frame_inventory_step(label: str, **fields: Any) -> CommandStep:
    clean_fields = {key: value for key, value in fields.items() if value is not None}
    return _step('table_frame_inventory', label, **clean_fields)


def _split_step_fields(raw: str) -> tuple[str, dict[str, str]]:
    parts = [part.strip() for part in raw.replace(',', ';').split(';') if part.strip()]
    if not parts:
        raise BundleError('Raw step spec must not be empty.')
    op = parts[0].strip()
    fields: dict[str, str] = {}
    for part in parts[1:]:
        if '=' not in part:
            raise BundleError(f'Raw step field must use key=value syntax: {part!r}')
        key, value = part.split('=', 1)
        key = key.strip().replace('-', '_')
        if not key:
            raise BundleError(f'Raw step field key is empty: {part!r}')
        fields[key] = value.strip()
    return op, fields


def _parse_bundle_args(parser: argparse.ArgumentParser, argv: Sequence[str]) -> argparse.Namespace:
    try:
        return parser.parse_args(list(argv))
    except SystemExit as exc:
        raise BundleError(f'Invalid bundle arguments. Run `hwpx bundle-help {parser.prog.split()[-1]}` for usage.') from exc


def _build_where(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('where', 'Return current live document/caret/selection proof.')
    _parse_bundle_args(parser, argv)
    return BundleSpec(
        name='where',
        summary='Read-only current location proof.',
        where='Current active live document session, caret, selection, and table-cell summary.',
        how='Runs the primitive `where` step only.',
        changed='No document content change; read-only proof.',
        steps=(_where_step(),),
    )


def _build_context(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('context', 'Return structured current edit-position context for local formatting and LLM use.')
    _parse_bundle_args(parser, argv)
    return BundleSpec(
        name='context',
        summary='Read-only structured edit-position context.',
        where='Current active live document edit position, page, nearby text, table/cell state, and style summary.',
        how='Runs the primitive `context` step only; server returns structured observation data, local CLI formats it.',
        changed='No document content change; read-only context card.',
        steps=(_context_step(),),
    )


def _readback_bundle_spec(argv: Sequence[str], *, name: str, summary: str, default_scope: str) -> BundleSpec:
    parser = _parser(name, 'Return bounded LLM-friendly HWPX/Hancom readback for caret/selection/page/document scopes.')
    parser.add_argument('--scope', choices=('caret', 'selection', 'page', 'document'), default=default_scope)
    parser.add_argument('--page-from', type=int, help='Optional first 1-based page for page/range readback')
    parser.add_argument('--page-to', type=int, help='Optional last 1-based page for page/range readback')
    parser.add_argument('--max-blocks', type=int, default=300, help='Maximum outside-text blocks in compact JSON')
    parser.add_argument('--max-table-cells', type=int, default=800, help='Maximum table cells in compact JSON')
    parser.add_argument('--max-controls', type=int, default=2048, help='Maximum controls/images/tables in compact JSON')
    args = _parse_bundle_args(parser, argv)
    for key in ('page_from', 'page_to', 'max_blocks', 'max_table_cells', 'max_controls'):
        value = getattr(args, key, None)
        if value is not None and value <= 0:
            raise BundleError(f'--{key.replace("_", "-")} must be a positive integer.')
    if args.page_from is not None and args.page_to is not None and args.page_to < args.page_from:
        raise BundleError('--page-to must be >= --page-from.')
    fields: dict[str, Any] = {
        'scope': args.scope,
        'max_blocks': int(args.max_blocks),
        'max_table_cells': int(args.max_table_cells),
        'max_controls': int(args.max_controls),
    }
    if args.page_from is not None:
        fields['page_from'] = int(args.page_from)
    if args.page_to is not None:
        fields['page_to'] = int(args.page_to)
    return BundleSpec(
        name=name,
        summary=summary,
        where='Current active live Hancom document session; scope controls caret/selection/current page/document readback.',
        how='Runs the readback command-bundle step; server writes raw evidence to an artifact and returns bounded compact JSON.',
        changed='No document content change; read-only LLM-friendly readback.',
        steps=(_step('readback', f'readback:{args.scope}', **fields),),
    )


def _build_readback(argv: Sequence[str]) -> BundleSpec:
    return _readback_bundle_spec(argv, name='readback', summary='Read-only bounded LLM-friendly HWPX/Hancom readback.', default_scope='caret')


def _build_read_context(argv: Sequence[str]) -> BundleSpec:
    return _readback_bundle_spec(argv, name='read-context', summary='Alias for readback: bounded LLM-friendly current-context readback.', default_scope='caret')


def _build_read_manifest(argv: Sequence[str]) -> BundleSpec:
    return _readback_bundle_spec(argv, name='read-manifest', summary='Read-only readback with raw artifact manifest path.', default_scope='document')


def _build_typography_overview(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('typography-overview', 'Read-only live Hancom/HWPML typography overview for the active document.')
    parser.add_argument('--scope', choices=('document',), default='document')
    parser.add_argument('--max-samples', type=int, default=40, help='Maximum sample bucket budget')
    parser.add_argument('--max-sections', type=int, default=30, help='Maximum section overview entries')
    parser.add_argument('--max-styles', type=int, default=40, help='Maximum font/size/style rows')
    args = _parse_bundle_args(parser, argv)
    for key in ('max_samples', 'max_sections', 'max_styles'):
        value = getattr(args, key, None)
        if value is not None and value <= 0:
            raise BundleError(f'--{key.replace("_", "-")} must be a positive integer.')
    return BundleSpec(
        name='typography-overview',
        summary='Read-only active-document typography overview from live Hancom HWPML2X export.',
        where='Current active live Hancom document session.',
        how='Runs one `typography_overview` server primitive under the runtime lock. The server reads GetTextFile(HWPML2X), parses character-shape/style runs, writes a raw JSON artifact, and returns a compact summary.',
        changed='No document content change; read-only native typography/style evidence only.',
        steps=(
            _step(
                'typography_overview',
                'inspect:typography-overview',
                scope=args.scope,
                max_samples=int(args.max_samples),
                max_sections=int(args.max_sections),
                max_styles=int(args.max_styles),
            ),
        ),
    )


def _build_text_table_to_native(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('text-table-to-native', 'Convert a Markdown/pipe text table into a native HWP table insertion bundle.')
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument('--from-file', type=Path, help='UTF-8 file containing the Markdown/pipe table source')
    source_group.add_argument('--text', help='Inline Markdown/pipe table source')
    parser.add_argument(
        '--confirm-native-table',
        action='store_true',
        required=True,
        help='Required explicit confirmation: insert a native HWP table at the current caret; old pipe/plain text is not removed.',
    )
    parser.add_argument('--field-name', default='__rumi_native_table_cells__', help='Temporary field name used while filling the native table cells')
    parser.add_argument('--split-by-column', help='Split the parsed table into one native table per distinct value under this header; the header row is repeated in each split table')
    args = _parse_bundle_args(parser, argv)
    if not args.confirm_native_table:
        raise BundleError('text-table-to-native requires --confirm-native-table.')
    source_text, source = _read_table_text_from_args(args)
    cells = parse_pipe_table(source_text)
    field_name = str(args.field_name or '').strip()
    if not field_name:
        raise BundleError('text-table-to-native requires a non-empty --field-name.')
    warnings = [
        'Native table insertion requires rendered proof before any save/final delivery.',
        'Old pipe/plain source text cleanup is deferred; this bundle does not delete source text.',
    ]
    split_by_column = str(args.split_by_column or '').strip()
    if split_by_column:
        split_column_index, split_groups = _split_pipe_table_by_column(cells, split_by_column)
        split_count = len(split_groups)
        steps: list[CommandStep] = []
        sources: list[dict[str, Any]] = []
        split_warnings = [
            *warnings,
            'Split-by-column emits one native_table_insert step per group; full multi-table placement still requires live rendered proof before cleanup/save.',
        ]
        for split_index, (group_value, group_cells) in enumerate(split_groups, start=1):
            split_rows = len(group_cells)
            split_cols = len(group_cells[0])
            flat_values = [cell for row in group_cells for cell in row]
            non_empty = [cell for cell in flat_values if cell.strip()]
            split_hash = _hash_cell_value(f'{split_by_column}\n{group_value}\n{json.dumps(group_cells, ensure_ascii=False, separators=(",", ":"))}')
            split_metadata = {
                'split_by_column': split_by_column,
                'split_column_index': split_column_index,
                'split_group_value': group_value,
                'split_group_index': split_index,
                'split_group_count': split_count,
                'split_group_hash': split_hash,
            }
            steps.append(
                _step(
                    'native_table_insert',
                    f'native-table:split-{split_index:03d}',
                    rows=split_rows,
                    cols=split_cols,
                    cells=group_cells,
                    field_name=_field_name_for_split(field_name, split_index),
                    confirm_native_table=True,
                    source_text_deleted=False,
                    old_plain_text_removal='deferred_until_rendered_proof',
                    flat_value_hashes=[_hash_cell_value(value) for value in flat_values],
                    non_empty_token_count=len(non_empty),
                    non_empty_token_preview=non_empty[:8],
                    warnings=split_warnings,
                    next_proof_required=f'rendered proof must show split table {split_index}/{split_count} ({split_by_column}={group_value}), native gridlines, all value tokens, no clipping, and old text cleanup remains deferred.',
                )
            )
            sources.append(
                {
                    **source,
                    'split_by_column': split_by_column,
                    'split_column_index': split_column_index,
                    'split_group_value': group_value,
                    'split_group_index': split_index,
                    'split_group_count': split_count,
                    'split_group_hash': split_hash,
                    'rows': split_rows,
                    'cols': split_cols,
                    'source_text_deleted': False,
                    'old_plain_text_removal': 'deferred_until_rendered_proof',
                    'flat_value_hashes': [_hash_cell_value(value) for value in flat_values],
                    'non_empty_token_count': len(non_empty),
                    'non_empty_token_preview': non_empty[:8],
                    'warnings': split_warnings,
                    'next_proof_required': 'rendered proof before any old-text cleanup or save/final delivery',
                }
            )
        return BundleSpec(
            name='text-table-to-native',
            summary=f'Insert a parsed Markdown/pipe table as {split_count} split native HWP tables grouped by {split_by_column!r}.',
            where='Current live Hancom caret; caller must position it at the desired first native table insertion point and verify every split table in rendered proof.',
            how='Parses and splits the text table locally, repeats the header row for each group, then runs one native_table_insert step per split group using documented pyhwpx field-fill APIs.',
            changed='Split native HWP table insertions planned/executed; source_text_deleted=false; old_plain_text_removal=deferred_until_rendered_proof; full multi-table placement is accepted only after rendered proof.',
            steps=tuple(steps),
            sources=tuple(sources),
        )

    rows = len(cells)
    cols = len(cells[0])
    flat_values = [cell for row in cells for cell in row]
    non_empty = [cell for cell in flat_values if cell.strip()]
    step = _step(
        'native_table_insert',
        'native-table:insert-and-fill',
        rows=rows,
        cols=cols,
        cells=cells,
        field_name=field_name,
        confirm_native_table=True,
        source_text_deleted=False,
        old_plain_text_removal='deferred_until_rendered_proof',
        flat_value_hashes=[_hash_cell_value(value) for value in flat_values],
        non_empty_token_count=len(non_empty),
        non_empty_token_preview=non_empty[:8],
        warnings=warnings,
        next_proof_required='rendered proof must show native gridlines, all value tokens, no clipping, and old text cleanup remains deferred.',
    )
    return BundleSpec(
        name='text-table-to-native',
        summary='Insert a parsed Markdown/pipe table as a native HWP table at the current caret.',
        where='Current live Hancom caret; caller must position it at the desired native table insertion point.',
        how='Parses the text table locally, then runs native_table_insert using create_table_at_cursor and documented pyhwpx field-fill APIs.',
        changed='Native HWP table inserted and filled; source_text_deleted=false; old_plain_text_removal=deferred_until_rendered_proof.',
        steps=(step,),
        sources=(
            {
                **source,
                'rows': rows,
                'cols': cols,
                'source_text_deleted': False,
                'old_plain_text_removal': 'deferred_until_rendered_proof',
                'flat_value_hashes': [_hash_cell_value(value) for value in flat_values],
                'non_empty_token_count': len(non_empty),
                'non_empty_token_preview': non_empty[:8],
                'warnings': warnings,
                'next_proof_required': 'rendered proof before any old-text cleanup or save/final delivery',
            },
        ),
    )



def _build_text_table_cleanup_selected(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('text-table-cleanup-selected', 'Delete exactly the active selected old plain/source text after separate rendered proof.')
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument('--from-file', type=Path, help='UTF-8 file containing the exact expected selected/source text')
    source_group.add_argument('--text', help='Inline exact expected selected/source text')
    parser.add_argument(
        '--confirm-cleanup',
        action='store_true',
        required=True,
        help='Required explicit confirmation: delete the current active selection only if exact source/hash and non-table checks pass.',
    )
    parser.add_argument('--expected-hash', help='Optional expected sha256:<hex> of the exact source text; computed locally when omitted')
    parser.add_argument('--native-table-proof-ref', help='Optional rendered/native table proof label or artifact reference')
    parser.add_argument('--native-table-proof-hash', help='Optional hash/token for the separate native table proof artifact')
    args = _parse_bundle_args(parser, argv)
    if not args.confirm_cleanup:
        raise BundleError('text-table-cleanup-selected requires --confirm-cleanup.')
    source_text, source = _read_exact_text_from_args(args, command_name='text-table-cleanup-selected')
    if source_text == '':
        raise BundleError('text-table-cleanup-selected requires non-empty expected selected/source text.')
    if len(source_text) > 50000:
        raise BundleError('text-table-cleanup-selected source text is too long (max 50000 characters).')
    source_hash = _hash_cell_value(source_text)
    if args.expected_hash:
        expected_hash = str(args.expected_hash).strip().lower()
        if expected_hash != source_hash:
            raise BundleError(f'--expected-hash does not match source text (expected {source_hash}).')
    normalized_text = _normalize_visible_text(source_text)
    if not normalized_text:
        raise BundleError('text-table-cleanup-selected normalized source text is empty.')
    normalized_hash = _hash_cell_value(normalized_text)
    fields: dict[str, Any] = {
        'expected_text': source_text,
        'expected_hash': source_hash,
        'expected_normalized_hash': normalized_hash,
        'confirm_cleanup': True,
        'source_text_deleted': False,
    }
    if args.native_table_proof_ref:
        fields['native_table_proof_ref'] = str(args.native_table_proof_ref).strip()
    if args.native_table_proof_hash:
        fields['native_table_proof_hash'] = str(args.native_table_proof_hash).strip()
    warnings = [
        'Cleanup is separate from native table insertion; text-table-to-native never deletes old source text automatically.',
        'Selection must already be exact and outside table/cell context; this command fails closed otherwise.',
        'Rendered before/after proof is required before save/final delivery.',
    ]
    step = _step('selected_text_delete_exact', 'cleanup:selected-text-delete-exact', **fields)
    return BundleSpec(
        name='text-table-cleanup-selected',
        summary='Delete only the current active selected old plain/source text after exact source/hash and non-table checks.',
        where='Current active live Hancom selection; it must be exact plain text outside any table/cell context.',
        how='Runs one selected_text_delete_exact primitive; it reads selected text, checks normalized hash/text and table/cell context, then deletes the selection only after all gates pass.',
        changed='Deletes the active selected source text only; cleanup is separate from native insertion; rendered before/after proof remains required before save/final delivery.',
        steps=(step,),
        sources=(
            {
                **source,
                'expected_hash': source_hash,
                'expected_normalized_hash': normalized_hash,
                'source_text_deleted': False,
                'cleanup_separate_from_native_insertion': True,
                'selection_must_be_exact_and_outside_table_cell': True,
                'rendered_before_after_proof_required': True,
                'warnings': warnings,
            },
        ),
    )


def _build_table4_anchor_range_replace(argv: Sequence[str]) -> BundleSpec:
    parser = _parser(
        'table4-anchor-range-replace',
        'Replace the exact Table 4 text range (start anchor through before Figure 5) with a native HWP table.',
    )
    parser.add_argument('--from-file', type=Path, required=True, help='UTF-8 Markdown/pipe table source for the replacement native table')
    parser.add_argument('--section-anchor', required=True, help='Section anchor that must occur before the start anchor')
    parser.add_argument('--start-anchor', required=True, help='Range start anchor; included in the deleted old source text')
    parser.add_argument('--end-before-anchor', required=True, help='Range end anchor; the selection stops immediately before this anchor')
    parser.add_argument('--required-source-basename', help='Fail closed unless the active live source filename has this basename')
    parser.add_argument('--forbid-source-basename', help='Fail closed if the active live source filename has this basename')
    parser.add_argument('--expected-range-hash', help='Optional sha256:<hex> exact selected range hash guard')
    parser.add_argument('--expected-normalized-range-hash', help='Optional sha256:<hex> normalized selected range hash guard')
    parser.add_argument('--caption-text', help='Optional caption text to insert immediately before the native table after deleting the selected range')
    parser.add_argument('--field-name', default='__rumi_table4_native_cells__', help='Temporary field name used while filling the native table cells')
    parser.add_argument(
        '--confirm-replace',
        action='store_true',
        required=True,
        help='Required explicit confirmation: delete the selected old Table 4 range and insert a native table under one live runtime lock.',
    )
    args = _parse_bundle_args(parser, argv)
    if not args.confirm_replace:
        raise BundleError('table4-anchor-range-replace requires --confirm-replace.')
    table_path = Path(args.from_file).expanduser()
    if not table_path.exists() or not table_path.is_file():
        raise BundleError(f'table source file not found: {table_path}')
    source_text = table_path.read_text(encoding='utf-8')
    source = {'type': 'from-file', 'path': str(table_path)}
    cells = parse_pipe_table(source_text)
    rows = len(cells)
    cols = len(cells[0])
    if cols != 4:
        raise BundleError('table4-anchor-range-replace requires a compact 4-column pipe table source.')
    field_name = str(args.field_name or '').strip()
    if not field_name:
        raise BundleError('table4-anchor-range-replace requires a non-empty --field-name.')
    flat_values = [cell for row in cells for cell in row]
    non_empty = [cell for cell in flat_values if cell.strip()]
    warnings = [
        'This primitive deletes only the live selected range proven by section/start/end anchors, then inserts the native table at the same live position.',
        'Rendered proof must show Table 4, Figure 5, section continuity, no duplicate old Table 4 source block, and no clipping before save/final delivery.',
    ]
    fields: dict[str, Any] = {
        'section_anchor': str(args.section_anchor).strip(),
        'start_anchor': str(args.start_anchor).strip(),
        'end_before_anchor': str(args.end_before_anchor).strip(),
        'rows': rows,
        'cols': cols,
        'cells': cells,
        'field_name': field_name,
        'confirm_replace': True,
        'flat_value_hashes': [_hash_cell_value(value) for value in flat_values],
        'non_empty_token_count': len(non_empty),
        'non_empty_token_preview': non_empty[:8],
        'warnings': warnings,
        'next_proof_required': 'rendered proof must cover Table 4, Figure 5, section continuity, old-source cleanup, native gridlines, all value tokens, and no clipping.',
    }
    for key, value in (
        ('required_source_basename', args.required_source_basename),
        ('forbid_source_basename', args.forbid_source_basename),
        ('expected_range_hash', args.expected_range_hash),
        ('expected_normalized_range_hash', args.expected_normalized_range_hash),
        ('caption_text', args.caption_text),
    ):
        if value:
            fields[key] = str(value).strip()
    step = _step('anchor_range_replace_native_table', 'table4:anchor-range-replace-native', **fields)
    return BundleSpec(
        name='table4-anchor-range-replace',
        summary='Replace the guarded Table 4 text range with a compact four-column native HWP table.',
        where='Active live Hancom working copy; section anchor must precede the Table 4 start anchor, and Figure 5 must be the next end-before anchor.',
        how='Server resolves anchors under one runtime lock, selects/hashes the exact start→before-end range, deletes it only after gates pass, then inserts/fills a native HWP table at the deletion point.',
        changed='Deletes the old Table 4 source text range and inserts a native HWP table; no auto-save; rendered proof is required before final acceptance.',
        steps=(step,),
        sources=(
            {
                **source,
                'rows': rows,
                'cols': cols,
                'source_text_deleted': 'runtime_proven_by_anchor_range_replace_native_table',
                'flat_value_hashes': [_hash_cell_value(value) for value in flat_values],
                'non_empty_token_count': len(non_empty),
                'non_empty_token_preview': non_empty[:8],
                'warnings': warnings,
                'next_proof_required': fields['next_proof_required'],
            },
        ),
    )


def _build_selected_text_proof(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('selected-text-proof', 'Return selected text proof plus before/after location.')
    parser.add_argument(
        '--clear-selection',
        action='store_true',
        help='Ask the server not to keep the current selection after reading selected text.',
    )
    args = _parse_bundle_args(parser, argv)
    keep_select = not bool(args.clear_selection)
    return BundleSpec(
        name='selected-text-proof',
        summary='Read-only selected-text proof with location snapshots.',
        where='Current active selection in the live document session.',
        how='Runs `where`, then `get_selected_text`, then `where` again.',
        changed='No content change; selection is preserved unless --clear-selection is used.',
        steps=(
            _where_step('where:before-selection-read'),
            _step('get_selected_text', 'proof:selected-text', keep_select=keep_select),
            _where_step('where:after-selection-read'),
        ),
    )


def _build_selection_proof(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('selection-proof', 'Return robust read-only selection proof with boundary and risk evidence.')
    _parse_bundle_args(parser, argv)
    return BundleSpec(
        name='selection-proof',
        summary='Read-only proof of the active selection, boundary context, and risk flags.',
        where='Current active Hancom selection in the live document session.',
        how='Runs the primitive `selection_proof` step only; server returns structured evidence, local CLI formats it.',
        changed='No document content change; selection/position is restored after best-effort probes.',
        steps=(_step('selection_proof', 'selection-proof:active-selection'),),
    )


def _build_insert_text_file(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('insert-text-file', 'Insert UTF-8 text from a local file at the active target.')
    parser.add_argument('text_file', type=Path, help='UTF-8 text file to insert via set_text_file')
    parser.add_argument(
        '--ack-active-target',
        action='store_true',
        help='Required: confirms the operator already selected or positioned the intended target.',
    )
    args = _parse_bundle_args(parser, argv)
    if not args.ack_active_target:
        raise BundleError('insert-text-file requires --ack-active-target to avoid accidental caret insertion.')
    text_file = args.text_file.expanduser()
    if not text_file.exists() or not text_file.is_file():
        raise BundleError(f'Text file not found: {args.text_file}')
    text = text_file.read_text(encoding='utf-8')
    if not text:
        raise BundleError(f'Text file is empty: {args.text_file}')
    return BundleSpec(
        name='insert-text-file',
        summary='Insert a UTF-8 text file at the active target.',
        where='Active live selection or caret chosen before running this bundle.',
        how='Runs `where`, then `set_text_file(format=UNICODE, option=insertfile)`, then `where`.',
        changed='Document content changes at the active target; proof comes from before/after location and server dirty flag.',
        steps=(
            _where_step('where:before-insert'),
            _step('set_text_file', 'insert:text-file', text=text, format='UNICODE', option='insertfile'),
            _where_step('where:after-insert'),
        ),
    )


_CREATE_RAW_SAFE_OPS = frozenset({'where', 'get_selected_text', 'set_text_file'})
_CREATE_RAW_BLOCKED_OPS = frozenset({'delete', 'erase', 'table-delete', 'table_delete', 'cell-clear-contents', 'cell_clear_contents'})


def parse_raw_bundle_step(raw: str) -> CommandStep:
    """Parse a local create-bundle raw primitive step spec.

    Syntax uses semicolon-separated fields to avoid adding server policy:
    - where[;label=...]
    - get_selected_text[;label=...][;keep_select=true|false]
    - set_text_file;path=body.txt;ack_active_target=true[;label=...]
    """
    op, fields = _split_step_fields(raw)
    op = op.strip()
    normalized = op.replace('_', '-').lower()
    if normalized in _CREATE_RAW_BLOCKED_OPS or normalized.startswith(('delete', 'erase')):
        raise BundleError(f'Unsafe raw step op is blocked: {op!r}')
    if op not in _CREATE_RAW_SAFE_OPS:
        safe = ', '.join(sorted(_CREATE_RAW_SAFE_OPS))
        raise BundleError(f'Unsupported raw step op: {op!r}. Safe raw ops: {safe}')

    label = fields.pop('label', '').strip()
    if op == 'where':
        if fields:
            raise BundleError(f'Unsupported fields for raw where step: {", ".join(sorted(fields))}')
        return _where_step(label or 'where:create-bundle')

    if op == 'get_selected_text':
        keep_select = True
        if 'keep_select' in fields:
            keep_select = _coerce_bool(fields.pop('keep_select'), field='keep_select')
        if fields:
            raise BundleError(f'Unsupported fields for raw get_selected_text step: {", ".join(sorted(fields))}')
        return _step('get_selected_text', label or 'proof:selected-text', keep_select=keep_select)

    if op == 'set_text_file':
        ack_raw = fields.pop('ack_active_target', fields.pop('ack', '')).strip()
        if not ack_raw or not _coerce_bool(ack_raw, field='ack_active_target'):
            raise BundleError('Raw set_text_file requires ack_active_target=true to avoid accidental caret insertion.')
        path_raw = fields.pop('path', fields.pop('text_file', '')).strip()
        text_raw = fields.pop('text', '')
        if path_raw and text_raw:
            raise BundleError('Raw set_text_file accepts either path/text_file or text, not both.')
        if path_raw:
            text_file = Path(path_raw).expanduser()
            if not text_file.exists() or not text_file.is_file():
                raise BundleError(f'Text file not found: {path_raw}')
            text = text_file.read_text(encoding='utf-8')
        else:
            text = text_raw
        if not text:
            raise BundleError('Raw set_text_file requires non-empty text or text_file/path.')
        fmt = fields.pop('format', 'UNICODE').strip().upper()
        option = fields.pop('option', 'insertfile').strip().lower()
        if fmt != 'UNICODE' or option != 'insertfile':
            raise BundleError('Raw set_text_file only supports format=UNICODE and option=insertfile.')
        if fields:
            raise BundleError(f'Unsupported fields for raw set_text_file step: {", ".join(sorted(fields))}')
        return _step('set_text_file', label or 'insert:raw-text-file', text=text, format=fmt, option=option)

    raise BundleError(f'Unsupported raw step op: {op!r}')


def _source_for_recipe(name: str, argv: Sequence[str]) -> dict[str, Any]:
    source: dict[str, Any] = {'type': 'recipe', 'name': name}
    if argv:
        source['args'] = list(argv)
    return source


def _source_for_step(raw: str, step: CommandStep) -> dict[str, Any]:
    return {'type': 'raw-step', 'op': step.op, 'label': step.label, 'spec': raw}


def build_created_bundle(recipe_specs: Sequence[str], raw_step_specs: Sequence[str]) -> BundleSpec:
    """Build a local create-bundle composition without executing it."""
    steps: list[CommandStep] = []
    sources: list[dict[str, Any]] = []

    for spec in recipe_specs:
        argv = shlex.split(spec)
        if not argv:
            raise BundleError('Recipe spec must include a recipe name.')
        name, recipe_argv = argv[0], argv[1:]
        bundle = build_named_bundle(name, recipe_argv)
        steps.extend(bundle.steps)
        sources.append(_source_for_recipe(name, recipe_argv))

    for raw in raw_step_specs:
        step = parse_raw_bundle_step(raw)
        steps.append(step)
        sources.append(_source_for_step(raw, step))

    if not steps:
        raise BundleError('create-bundle requires at least one --recipe or --step.')

    recipe_count = sum(1 for source in sources if source.get('type') == 'recipe')
    raw_count = sum(1 for source in sources if source.get('type') == 'raw-step')
    return BundleSpec(
        name='created-bundle',
        summary=f'Local create-bundle composition ({recipe_count} recipes, {raw_count} raw steps).',
        where='Where proof is provided by included where/recipe steps; no server-side planner policy is added.',
        how='Built locally from explicit registry recipe specs and/or safe raw primitive step specs.',
        changed='Depends on included steps; where/get_selected_text are read-only, set_text_file changes the active acknowledged target.',
        steps=tuple(steps),
        sources=tuple(sources),
    )


def _build_anchor_insert_named(command_name: str, position: str, argv: Sequence[str]) -> BundleSpec:
    parser = _parser(command_name, f'Insert text with one deterministic anchor resolution ({position}).')
    parser.add_argument('--target', '--anchor', dest='target', required=True, help='Target text/heading anchor')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--text', help='Inline text to insert')
    source.add_argument('--from-file', type=Path, help='UTF-8 text file to insert')
    args = parser.parse_args(list(argv))
    text = args.text
    if args.from_file is not None:
        path = args.from_file.expanduser()
        if not path.exists() or not path.is_file():
            raise BundleError(f'Text file not found: {path}')
        text = path.read_text(encoding='utf-8')
    if not isinstance(text, str) or text == '':
        raise BundleError(f'{command_name} requires non-empty text')
    target = _bounded_text(args.target, field='target', max_chars=500)
    return BundleSpec(
        name=command_name,
        summary=f'Insert text {position} with one live anchor resolution to prevent repeated-before ordering reversal.',
        where=f'target anchor={target!r}',
        how=f'anchor_insert position={position}; server resolves anchor once then inserts combined text under one runtime lock',
        changed='working copy text inserted; no auto-save; rendered proof required',
        steps=(
            _step('anchor_insert', f'{command_name}:anchor-insert', target=target, position=position, text=text),
            _where_step(f'{command_name}:where-after'),
        ),
        sources=({'type': 'recipe', 'name': command_name},),
    )


def _build_insert_before_anchor(argv: Sequence[str]) -> BundleSpec:
    return _build_anchor_insert_named('insert-before-anchor', 'before-anchor', argv)


def _build_insert_after_anchor(argv: Sequence[str]) -> BundleSpec:
    return _build_anchor_insert_named('insert-after-anchor', 'after-anchor', argv)


def _build_insert_after_paragraph(argv: Sequence[str]) -> BundleSpec:
    return _build_anchor_insert_named('insert-after-paragraph', 'after-paragraph', argv)


def _build_insert_before_heading(argv: Sequence[str]) -> BundleSpec:
    return _build_anchor_insert_named('insert-before-heading', 'before-heading', argv)


def _build_cell_proof(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('cell-proof', 'Select/prove the table cell at the current caret position.')
    _parse_bundle_args(parser, argv)
    return BundleSpec(
        name='cell-proof',
        summary='Non-content-changing table-cell selection proof.',
        where='Table cell at the current caret position in the active live document.',
        how='Runs `where`, `HAction.Run(TableCellBlock)`, `get_selected_text`, and `where`.',
        changed='No document content change; selection may change to the current table cell.',
        steps=(
            _where_step('where:before-cell-proof'),
            _step('hwp_action', 'select:current-cell', action_name='TableCellBlock'),
            _step('get_selected_text', 'proof:cell-selected-text', keep_select=True),
            _where_step('where:after-cell-proof'),
        ),
    )


def _add_section_scope_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--section-anchor', help='Section heading/anchor text used as read-only scope evidence')
    group.add_argument('--page-from', type=int, help='First 1-based page to include')
    parser.add_argument('--page-to', type=int, help='Last 1-based page to include; requires or follows --page-from')
    parser.add_argument('--around', help='Optional nearby anchor/token to include as additional inventory evidence')
    parser.add_argument('--max-controls', type=int, default=2048, help='Maximum controls to enumerate before failing closed')


def _normalize_section_scope_args(args: argparse.Namespace) -> dict[str, Any]:
    section_anchor = _bounded_text(getattr(args, 'section_anchor', None), field='section_anchor')
    around = _bounded_text(getattr(args, 'around', None), field='around')
    page_from = _positive_int(getattr(args, 'page_from', None), field='page_from')
    page_to = _positive_int(getattr(args, 'page_to', None), field='page_to')
    if page_to is not None and page_from is None:
        raise BundleError('--page-to requires --page-from.')
    if page_from is not None and page_to is None:
        page_to = page_from
    if page_from is not None and page_to is not None and page_to < page_from:
        raise BundleError('--page-to must be greater than or equal to --page-from.')
    max_controls = _positive_int(getattr(args, 'max_controls', None), field='max_controls') or 2048
    return {
        'section_anchor': section_anchor,
        'page_from': page_from,
        'page_to': page_to,
        'around': around,
        'max_controls': max_controls,
    }


def _build_section_control_inventory(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('section-control-inventory', 'Read-only inventory of controls/images/tables/frames by section anchor or page range.')
    _add_section_scope_args(parser)
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    if scope['section_anchor']:
        where = f"Controls near section anchor {scope['section_anchor']!r}; page filter applies if page evidence is available."
    else:
        where = f"Controls whose anchor page is in pages {scope['page_from']}-{scope['page_to']}."
    return BundleSpec(
        name='section-control-inventory',
        summary='Read-only section/page-scoped control inventory.',
        where=where,
        how='Runs one thin `control_inventory` server primitive; local parser formats target ids, hashes, page/bounds evidence, and warnings.',
        changed='No document content change; read-only proof only.',
        steps=(
            _control_inventory_step('inventory:section-controls', **scope),
        ),
    )


def _build_section_table_frame_inventory(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('section-table-frame-inventory', 'Read-only table/frame-flow inventory with shape properties and co-anchored groups.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', help='Optional exact target id/path from section-control-inventory')
    parser.add_argument('--expected-hash', help='Optional exact proof_hash for target proof')
    parser.add_argument('--expected-page', type=int, help='Optional expected page for target proof')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id') if args.target_id else None
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash') if args.expected_hash else None
    expected_page = _positive_int(args.expected_page, field='expected_page')
    if (target_id or expected_hash or expected_page) and not (target_id and expected_hash and expected_page):
        raise BundleError('--target-id, --expected-hash, and --expected-page must be supplied together when target proof is requested.')
    if scope['section_anchor']:
        where = f"Table/frame-flow controls near section anchor {scope['section_anchor']!r}; page filter applies if page evidence is available."
    else:
        where = f"Table/frame-flow controls whose anchor page is in pages {scope['page_from']}-{scope['page_to']}."
    return BundleSpec(
        name='section-table-frame-inventory',
        summary='Read-only table/frame-flow inventory with co-anchored control grouping.',
        where=where,
        how='Runs one thin `table_frame_inventory` primitive; local parser formats anchor groups, shape properties, fit-risk notes, target ids, hashes, and warnings.',
        changed='No document content change; read-only proof only.',
        steps=(
            _table_frame_inventory_step(
                'inventory:section-table-frame-flow',
                target_id=target_id,
                expected_hash=expected_hash,
                expected_page=expected_page,
                **scope,
            ),
        ),
    )


def _build_export_proof_range(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('export-proof-range', 'Export the active working copy to PDF for local page rendering/manifest generation.')
    parser.add_argument('--pages', help='1-based page range expression, e.g. 19-25 or 19-21,23')
    parser.add_argument('--all-pages', action='store_true', help='Public command should render every page in the fresh PDF')
    parser.add_argument('--dpi', type=int, default=160, help='Local render DPI; validated by the public command')
    parser.add_argument('--out-dir', help='Local proof output directory; validated by the public command')
    parser.add_argument('--anchor', action='append', default=[], help='Optional token to verify in rendered/exported text manifest')
    parser.add_argument('--section-anchor', help='Optional PDF-text anchor used locally to derive proof pages')
    parser.add_argument('--until-anchor', help='Optional later PDF-text anchor that ends the derived proof range')
    parser.add_argument('--fresh-session', action='store_true', help='Public command may reopen a saved HWP before running this export')
    parser.add_argument('--source-hwp', help='Public command source path for --fresh-session; local bundle metadata only')
    parser.add_argument('--contact-sheet', action='store_true', help='Public command may create a contact sheet')
    _parse_bundle_args(parser, argv)
    return BundleSpec(
        name='export-proof-range',
        summary='Bundle-backed PDF export for local page-range proof rendering.',
        where='Active live document session bound to the current local CLI state.',
        how='Runs one thin `export_pdf` server primitive; local command downloads the PDF, renders requested pages, and writes manifest JSON.',
        changed='No content change; export/proof artifacts only.',
        steps=(
            _step('export_pdf', 'export:fresh-pdf'),
        ),
    )


def _build_style_inspect(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('style-inspect', 'Read-only character/paragraph/list style inspection at a match or current caret.')
    parser.add_argument('match', nargs='?', help='Optional text match/anchor to inspect before restoring the original caret')
    parser.add_argument('--keep-position', action='store_true', help='Do not restore the original caret after inspecting a match')
    args = _parse_bundle_args(parser, argv)
    match = _bounded_text(args.match, field='match') if args.match else None
    return BundleSpec(
        name='style-inspect',
        summary='Read-only style inspection for character, paragraph, and list-like properties.',
        where=f'Match {match!r} in the active live document.' if match else 'Current caret/selection in the active live document.',
        how='Runs one thin `style_inspect` primitive. The server only reads native style/default parameters under the runtime lock; local output formats the result.',
        changed='No document content change; read-only style proof only.',
        steps=(
            _step('style_inspect', 'inspect:style', match=match, keep_position=bool(args.keep_position)),
        ),
    )


def _build_paragraph_style_apply_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('paragraph-style-apply-exact', 'Apply exact paragraph style toggles to one text match on the expected page.')
    parser.add_argument('--match', required=True, help='Unique paragraph text/anchor to style')
    parser.add_argument('--expected-page', type=int, required=True, help='Required rendered/page evidence for the target before mutation')
    parser.add_argument('--keep-with-next', choices=('on', 'off'), help='Set native KeepWithNext paragraph flag')
    parser.add_argument('--widow-orphan', choices=('on', 'off'), help='Set native WidowOrphan paragraph flag')
    parser.add_argument('--pagebreak-before', type=int, choices=(0, 1), help='Set native PagebreakBefore paragraph flag to 0 or 1')
    parser.add_argument('--confirm-layout', action='store_true', required=True, help='Required explicit confirmation for paragraph layout/style mutation')
    args = _parse_bundle_args(parser, argv)
    match = _bounded_text(args.match, field='match')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    fields: dict[str, Any] = {
        'match': match,
        'expected_page': expected_page,
        'confirm_layout': bool(args.confirm_layout),
    }
    op_bits: list[str] = []
    if args.keep_with_next is not None:
        fields['keep_with_next'] = _coerce_bool(args.keep_with_next, field='keep_with_next')
        op_bits.append(f"keep_with_next={args.keep_with_next}")
    if args.widow_orphan is not None:
        fields['widow_orphan'] = _coerce_bool(args.widow_orphan, field='widow_orphan')
        op_bits.append(f"widow_orphan={args.widow_orphan}")
    if args.pagebreak_before is not None:
        fields['pagebreak_before'] = int(args.pagebreak_before)
        op_bits.append(f"pagebreak_before={int(args.pagebreak_before)}")
    if not op_bits:
        raise BundleError('paragraph-style-apply-exact requires at least one style field.')
    operation = ', '.join(op_bits)
    return BundleSpec(
        name='paragraph-style-apply-exact',
        summary='Apply exact paragraph style toggles to one matched paragraph; fail closed on match/page/layout confirmation.',
        where=f'Match {match!r} on expected page {expected_page}.',
        how='Runs read-only where, then one `paragraph_style_apply_exact` primitive that finds the match on expected_page and applies only requested native paragraph style fields.',
        changed=f'Mutates matched paragraph style only if match, expected_page, and confirm_layout all pass; requested {operation}.',
        steps=(
            _where_step('where:before-paragraph-style'),
            _step('paragraph_style_apply_exact', 'mutate:paragraph-style-apply-exact', operation=operation, **fields),
            _where_step('where:after-paragraph-style'),
        ),
        sources=(
            {
                'type': 'paragraph-style-mutation',
                'operation': operation,
                'risk_note': 'Commit does not save automatically; accept only after rendered before/after proof on a disposable or working copy.',
            },
        ),
    )


def _build_paragraph_delete_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('paragraph-delete-exact', 'Delete one exact paragraph/text row after page and neighbor proof.')
    parser.add_argument('--match', required=True, help='Exact text/anchor in the paragraph to remove, e.g. a hyphen-only scaffold cell/row')
    parser.add_argument('--expected-page', type=int, required=True, help='Required rendered/page evidence for the target before mutation')
    parser.add_argument('--occurrence-on-page', type=int, default=1, help='1-based occurrence among targets that satisfy page/neighbor guards')
    parser.add_argument('--expected-previous-contains', help='Optional guard token that must appear in the previous paragraph preview')
    parser.add_argument('--expected-next-contains', help='Optional guard token that must appear in the next paragraph preview')
    parser.add_argument('--max-page-after', type=int, help='Optional maximum page evidence accepted after cleanup')
    parser.add_argument('--confirm-remove', action='store_true', required=True, help='Required explicit confirmation for destructive paragraph cleanup')
    args = _parse_bundle_args(parser, argv)
    match = _bounded_text(args.match, field='match')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    occurrence = _positive_int(args.occurrence_on_page, field='occurrence_on_page')
    fields: dict[str, Any] = {
        'match': match,
        'expected_page': expected_page,
        'occurrence_on_page': occurrence,
        'confirm_remove': bool(args.confirm_remove),
    }
    guard_bits: list[str] = []
    if args.expected_previous_contains:
        fields['expected_previous_contains'] = _bounded_text(args.expected_previous_contains, field='expected_previous_contains')
        guard_bits.append('previous-context')
    if args.expected_next_contains:
        fields['expected_next_contains'] = _bounded_text(args.expected_next_contains, field='expected_next_contains')
        guard_bits.append('next-context')
    if args.max_page_after is not None:
        fields['max_page_after'] = _positive_int(args.max_page_after, field='max_page_after')
        guard_bits.append(f"max_page_after={fields['max_page_after']}")
    operation = f'delete exact paragraph match occurrence={occurrence}; guards={", ".join(guard_bits) if guard_bits else "page-only"}'
    return BundleSpec(
        name='paragraph-delete-exact',
        summary='Delete one matched paragraph/row only after exact page and optional neighbor proof; fail closed on ambiguity.',
        where=f'Match {match!r} on expected page {expected_page}, occurrence {occurrence} after guard filtering.',
        how='Runs read-only where, then one `paragraph_delete_exact` primitive that locates the match on expected_page, verifies optional neighbor context, deletes the selected paragraph/row, and records before/after visible-text hashes.',
        changed='Mutates exactly one matched paragraph/row if page, occurrence, and context guards pass; rendered before/after proof remains required before save/final delivery.',
        steps=(
            _where_step('where:before-paragraph-delete'),
            _step('paragraph_delete_exact', 'mutate:paragraph-delete-exact', operation=operation, **fields),
            _where_step('where:after-paragraph-delete'),
        ),
        sources=(
            {
                'type': 'paragraph-cleanup-mutation',
                'operation': operation,
                'risk_note': 'Designed for hyphen-only scaffold cleanup; do not use for official heading hyphens such as 2-1) without rendered proof.',
            },
        ),
    )


def _build_style_apply_named(command_name: str, argv: Sequence[str]) -> BundleSpec:
    parser = _parser(command_name, 'Guarded dump-only spec for future style clone/apply mutation.')
    parser.add_argument('--from', dest='from_match', required=True, help='Exemplar match/anchor whose style should be copied')
    parser.add_argument('--to', '--to-match', dest='to_match', required=True, help='Target match/anchor for future style application')
    parser.add_argument('--dump-spec', action='store_true', help='Document that this is a guarded read-only proof spec')
    args = _parse_bundle_args(parser, argv)
    from_match = _bounded_text(args.from_match, field='from')
    to_match = _bounded_text(args.to_match, field='to')
    return BundleSpec(
        name=command_name,
        summary='GUARDED/DUMP-ONLY: inspect exemplar and target styles; does not mutate.',
        where=f'Exemplar {from_match!r} and target {to_match!r} in the active live document.',
        how='Runs two read-only `style_inspect` primitives. Live style copy remains blocked until selection/range targeting and rollback proof are validated.',
        changed='수정 안 됨 / no mutation performed. No mutation is implemented or executed by this guarded bundle.',
        steps=(
            _step('style_inspect', 'inspect:style-source', match=from_match, keep_position=False),
            _step('style_inspect', 'inspect:style-target', match=to_match, keep_position=False),
        ),
        sources=(
            {
                'type': f'guarded-{command_name}-spec',
                'blocked_reason': 'No validated native style clone/apply primitive with exact range proof, rollback semantics, and rendered before/after proof yet.',
                'from_match': from_match,
                'to_match': to_match,
            },
        ),
    )


def _build_style_apply(argv: Sequence[str]) -> BundleSpec:
    return _build_style_apply_named('style-apply', argv)


def _build_style_clone(argv: Sequence[str]) -> BundleSpec:
    return _build_style_apply_named('style-clone', argv)


def _build_qa_profile(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('qa-profile', 'Export a fresh PDF for local section-scoped token/freshness QA.')
    parser.add_argument('--section', '--section-anchor', dest='section_anchor', help='Optional section anchor for local PDF-text scoping')
    parser.add_argument('--until-anchor', help='Optional later anchor that ends the local section scope')
    parser.add_argument('--forbid', action='append', default=[], help='Forbidden token; repeatable')
    parser.add_argument('--require', action='append', default=[], help='Required token; repeatable')
    parser.add_argument('--after-anchor-forbid', action='append', default=[], help='ANCHOR::TOKEN pair checked locally after export')
    parser.add_argument('--source-hash', help='Expected source SHA256 or hash to compare locally')
    parser.add_argument('--out-dir', help='Local output directory; public command only')
    _parse_bundle_args(parser, argv)
    return BundleSpec(
        name='qa-profile',
        summary='Bundle-backed fresh PDF export for local token/order/source-hash QA.',
        where='Active live document session; local PDF text scoping may use section/until anchors.',
        how='Runs one thin `export_pdf` primitive. Local command downloads the fresh PDF, extracts text, and evaluates forbidden/required/order/freshness checks.',
        changed='No content change; QA artifacts only.',
        steps=(
            _step('export_pdf', 'export:qa-fresh-pdf'),
        ),
    )


def _build_section_frame_fill(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('section-frame-fill', 'Guarded dump-only target-proof skeleton for a future frame fill primitive.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Target id/path from section-control-inventory')
    parser.add_argument('--text-file', type=Path, required=True, help='UTF-8 text file intended for the future fill operation')
    parser.add_argument('--style-source', '--style-recipe', dest='style_source', required=True, help='Style source anchor/recipe to use after live proof exists')
    parser.add_argument('--expect-blank', action='store_true', help='Require target blankness in the future mutating primitive')
    parser.add_argument('--expect-token', help='Optional token expected in/near the target proof')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    text_file = args.text_file.expanduser()
    if not text_file.exists() or not text_file.is_file():
        raise BundleError(f'Text file not found: {args.text_file}')
    if not text_file.read_text(encoding='utf-8'):
        raise BundleError(f'Text file is empty: {args.text_file}')
    target_id = _bounded_text(args.target_id, field='target_id')
    return BundleSpec(
        name='section-frame-fill',
        summary='GUARDED/DUMP-ONLY: proves a frame target for a future fill; does not insert text.',
        where=f'Target {target_id!r} inside the requested section/page scope.',
        how='Currently emits only read-only `control_inventory` target proof. Live fill is blocked until a native frame insertion primitive has rendered disposable-document proof.',
        changed='수정 안 됨 / no mutation performed. No mutation is implemented or executed by this guarded bundle.',
        steps=(
            _control_inventory_step('guard:frame-fill-target-proof', target_id=target_id, **scope),
        ),
        sources=(
            {
                'type': 'guarded-mutation-spec',
                'blocked_reason': 'No validated native frame/textbox fill primitive with pre/post rendered proof yet.',
                'text_file': str(text_file),
                'style_source': str(args.style_source),
                'expect_blank': bool(args.expect_blank),
                'expect_token': args.expect_token,
            },
        ),
    )


def _build_section_graphic_remove_or_hide(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('section-graphic-remove-or-hide', 'Guarded dump-only target-proof skeleton for a future graphic remove/hide primitive.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Target id/path from section-control-inventory')
    parser.add_argument('--expected-hash', required=True, help='Expected target hash from inventory')
    parser.add_argument('--expected-page', type=int, help='Expected target page when page evidence is available')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--confirm-remove', action='store_true', help='Future destructive delete confirmation; currently still dump-only')
    mode.add_argument('--hide-only', action='store_true', help='Future non-destructive hide mode; currently still dump-only')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    return BundleSpec(
        name='section-graphic-remove-or-hide',
        summary='GUARDED/DUMP-ONLY: proves one graphic/control target for future remove/hide; does not delete or hide.',
        where=f'Target {target_id!r} inside the requested section/page scope with expected hash/page proof.',
        how='Currently emits only read-only `control_inventory` target proof. Live remove/hide is blocked until a one-control native primitive has rendered pre/post proof and fail-closed semantics.',
        changed='수정 안 됨 / no mutation performed. No mutation is implemented or executed by this guarded bundle.',
        steps=(
            _control_inventory_step(
                'guard:graphic-target-proof',
                target_id=target_id,
                expected_hash=expected_hash,
                expected_page=expected_page,
                **scope,
            ),
        ),
        sources=(
            {
                'type': 'guarded-mutation-spec',
                'blocked_reason': 'Delete/hide needs native one-control primitive, before/after rendered proof, and fail-closed ambiguity checks.',
                'requested_mode': 'confirm-remove' if args.confirm_remove else 'hide-only',
            },
        ),
    )


def _build_section_control_delete_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('section-control-delete-exact', 'Delete one control only after exact inventory proof matches.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Exact target id/path from section-control-inventory, e.g. ctrl/42/gso/no-inst')
    parser.add_argument('--expected-hash', required=True, help='Exact proof_hash from section-control-inventory')
    parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target')
    parser.add_argument('--confirm-remove', action='store_true', required=True, help='Required explicit confirmation for the one-control delete')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    return BundleSpec(
        name='section-control-delete-exact',
        summary='Delete exactly one pre-proven control; fail closed on target/hash/page/scope mismatch.',
        where=f'Target {target_id!r} inside the requested section/page scope, expected page {expected_page}.',
        how='Runs read-only where, then one `control_delete_exact` primitive that re-inventories controls and calls DeleteCtrl only for the exact matching control.',
        changed='Deletes one native control only if target id, proof_hash, expected page, and page/scope checks all match; server returns pre/post machine proof.',
        steps=(
            _where_step('where:before-control-delete'),
            _step(
                'control_delete_exact',
                'mutate:delete-exact-control',
                target_id=target_id,
                expected_hash=expected_hash,
                expected_page=expected_page,
                confirm_remove=bool(args.confirm_remove),
                **scope,
            ),
            _where_step('where:after-control-delete'),
        ),
        sources=(
            {
                'type': 'proven-mutation',
                'safety': 'exact target id + proof_hash + expected page + page/scope check; no broad selection/delete',
                'rollback': 'operate on disposable or working copies; discard the copy on failure',
            },
        ),
    )


def _add_exact_control_target_args(parser: argparse.ArgumentParser) -> None:
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Exact target id/path from section-control-inventory or section-table-frame-inventory')
    parser.add_argument('--expected-hash', required=True, help='Exact proof_hash from inventory')
    parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target before proof/mutation')


def _exact_control_target_fields(args: argparse.Namespace) -> tuple[dict[str, Any], str, str, int]:
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    fields: dict[str, Any] = {
        'target_id': target_id,
        'expected_hash': expected_hash,
        'expected_page': expected_page,
        **scope,
    }
    return fields, str(target_id), str(expected_hash), int(expected_page or 0)


def _build_exact_control_select_proof(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('exact-control-select-proof', 'Select one pre-proven control using native pyhwpx exact selection when available.')
    _add_exact_control_target_args(parser)
    args = _parse_bundle_args(parser, argv)
    fields, target_id, _expected_hash, expected_page = _exact_control_target_fields(args)
    return BundleSpec(
        name='exact-control-select-proof',
        summary='Read-only proof that one pre-proven control can be selected; prefers CtrlInstID + SelectCtrl on Hancom 2024.',
        where=f'Target {target_id!r} inside requested section/page scope, expected page {expected_page}.',
        how='Runs read-only where, then one `exact_control_select_proof` primitive that re-inventories controls and tries CtrlInstID/SelectCtrl before documented pyhwpx fallback selection.',
        changed='No document content/layout mutation; only the live editor selection/caret may change. Server returns selected-control proof and method_used.',
        steps=(
            _where_step('where:before-exact-control-select'),
            _step('exact_control_select_proof', 'proof:exact-control-select', **fields),
            _where_step('where:after-exact-control-select'),
        ),
        sources=(
            {
                'type': 'read-only-selection-proof',
                'safety': 'exact target id + proof_hash + expected page + page/scope check before selection proof',
                'fallback': 'falls back when CtrlInstID/SelectCtrl is unavailable and reports weaker proof strength explicitly',
            },
        ),
    )


def _build_cell_format_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('cell-format-exact', 'Apply first-pass native pyhwpx cell formatting after exact table inventory proof matches.')
    _add_exact_control_target_args(parser)
    format_group = parser.add_mutually_exclusive_group(required=True)
    format_group.add_argument('--cell-margin-hu', type=float, help='Set all current target-cell margins to this exact HWPUNIT value')
    format_group.add_argument('--cell-margin-mm', type=float, help='Set all current target-cell margins to this exact millimeter value')
    format_group.add_argument('--vertical-align', choices=('top', 'center', 'middle', 'bottom'), help='Apply native table-cell vertical alignment')
    format_group.add_argument('--fill-color', help='Apply an exact six-digit RGB fill color such as #12ABEF')
    format_group.add_argument('--border', choices=('none',), help='Remove all borders from the exact target cell')
    parser.add_argument('--confirm-layout', action='store_true', required=True, help='Required explicit confirmation for one-cell formatting mutation')
    args = _parse_bundle_args(parser, argv)
    fields, target_id, _expected_hash, expected_page = _exact_control_target_fields(args)
    fields['confirm_layout'] = bool(args.confirm_layout)
    if args.cell_margin_hu is not None:
        if not (0.0 <= float(args.cell_margin_hu) <= 20000.0):
            raise BundleError('--cell-margin-hu is outside the safe range.')
        fields['cell_margin_hu'] = float(args.cell_margin_hu)
        op_summary = f'cell_margin_hu={float(args.cell_margin_hu):g}'
    elif args.cell_margin_mm is not None:
        if not (0.0 <= float(args.cell_margin_mm) <= 70.0):
            raise BundleError('--cell-margin-mm is outside the safe range.')
        fields['cell_margin_mm'] = float(args.cell_margin_mm)
        op_summary = f'cell_margin_mm={float(args.cell_margin_mm):g}'
    elif args.fill_color is not None:
        fill_color = str(args.fill_color).strip().upper()
        if re.fullmatch(r'#[0-9A-F]{6}', fill_color) is None:
            raise BundleError('--fill-color must be a six-digit hex color like #12ABEF.')
        fields['fill_color'] = fill_color
        op_summary = f'fill_color={fill_color}'
    elif args.border is not None:
        fields['border'] = str(args.border)
        op_summary = f'border={args.border}'
    else:
        vertical_align = 'center' if args.vertical_align == 'middle' else str(args.vertical_align)
        fields['vertical_align'] = vertical_align
        op_summary = f'vertical_align={vertical_align}'
    return BundleSpec(
        name='cell-format-exact',
        summary='Apply exactly one pre-proven target-cell format change; fail closed on target/hash/page/scope mismatch.',
        where=f'Table target {target_id!r} inside requested section/page scope, expected page {expected_page}.',
        how='Runs read-only where, then one `cell_format_exact` primitive that enters the exact matching table cell and applies documented pyhwpx cell-margin or native TableVAlign action.',
        changed='Mutates one target table cell format only if target id, proof_hash, expected page, and page/scope checks all match; server returns pre/post metrics where available.',
        steps=(
            _where_step('where:before-cell-format'),
            _step('cell_format_exact', 'mutate:cell-format-exact', **fields),
            _where_step('where:after-cell-format'),
        ),
        sources=(
            {
                'type': 'layout-mutation',
                'operation': op_summary,
                'risk_note': 'Accept only after rendered before/after proof on a disposable or working copy.',
            },
        ),
    )


def _build_cell_row_fit_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('cell-row-fit-exact', 'Set one target table row height after exact table inventory proof matches.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Exact table target id/path from section-table-frame-inventory')
    parser.add_argument('--expected-hash', required=True, help='Exact proof_hash from section-table-frame-inventory')
    parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target before mutation')
    height_group = parser.add_mutually_exclusive_group(required=True)
    height_group.add_argument('--row-height-percent', type=float, help='Set the current table row height to this percent of its proven current height')
    height_group.add_argument('--row-height-hu', type=float, help='Set the current table row height to this exact HWPUNIT value')
    height_group.add_argument('--row-height-mm', type=float, help='Set the current table row height to this exact millimeter value')
    height_group.add_argument('--resize-up-steps', type=int, help='Run Hancom TableResizeUp this many times after exact target proof')
    height_group.add_argument('--resize-down-steps', type=int, help='Run Hancom TableResizeDown this many times after exact target proof')
    height_group.add_argument('--line-spacing', type=int, help='Apply paragraph line spacing to the selected target cell contents')
    height_group.add_argument('--char-height-percent', type=float, help='Scale selected target cell character height by this percent')
    parser.add_argument('--confirm-layout', action='store_true', required=True, help='Required explicit confirmation for one-row layout mutation')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    fields: dict[str, Any] = {
        'target_id': target_id,
        'expected_hash': expected_hash,
        'expected_page': expected_page,
        'confirm_layout': bool(args.confirm_layout),
        **scope,
    }
    if args.row_height_percent is not None:
        if not (20.0 <= float(args.row_height_percent) <= 120.0):
            raise BundleError('--row-height-percent must be between 20 and 120.')
        fields['row_height_percent'] = float(args.row_height_percent)
        op_summary = f'row_height={float(args.row_height_percent):g}%'
    elif args.row_height_hu is not None:
        if not (1000.0 <= float(args.row_height_hu) <= 200000.0):
            raise BundleError('--row-height-hu is outside the safe range.')
        fields['row_height_hu'] = float(args.row_height_hu)
        op_summary = f'row_height_hu={float(args.row_height_hu):g}'
    elif args.row_height_mm is not None:
        if not (3.0 <= float(args.row_height_mm) <= 700.0):
            raise BundleError('--row-height-mm is outside the safe range.')
        fields['row_height_mm'] = float(args.row_height_mm)
        op_summary = f'row_height_mm={float(args.row_height_mm):g}'
    elif args.resize_up_steps is not None:
        if not (1 <= int(args.resize_up_steps) <= 200):
            raise BundleError('--resize-up-steps must be 1..200.')
        fields['resize_up_steps'] = int(args.resize_up_steps)
        op_summary = f'resize_up_steps={int(args.resize_up_steps)}'
    elif args.resize_down_steps is not None:
        if not (1 <= int(args.resize_down_steps) <= 200):
            raise BundleError('--resize-down-steps must be 1..200.')
        fields['resize_down_steps'] = int(args.resize_down_steps)
        op_summary = f'resize_down_steps={int(args.resize_down_steps)}'
    elif args.line_spacing is not None:
        if not (80 <= int(args.line_spacing) <= 200):
            raise BundleError('--line-spacing must be 80..200.')
        fields['line_spacing'] = int(args.line_spacing)
        op_summary = f'line_spacing={int(args.line_spacing)}'
    else:
        if not (70.0 <= float(args.char_height_percent) <= 110.0):
            raise BundleError('--char-height-percent must be 70..110.')
        fields['char_height_percent'] = float(args.char_height_percent)
        op_summary = f'char_height_percent={float(args.char_height_percent):g}'
    return BundleSpec(
        name='cell-row-fit-exact',
        summary='Set exactly one pre-proven table row height; fail closed on target/hash/page/scope mismatch.',
        where=f'Table target {target_id!r} inside requested section/page scope, expected page {expected_page}.',
        how='Runs read-only where, then one `cell_row_fit_exact` primitive that enters the exact matching table and changes only the current row height via Hancom table property actions.',
        changed='Mutates one target table row/cell height only if target id, proof_hash, expected page, and page/scope checks all match; server returns pre/post row metrics proof.',
        steps=(
            _where_step('where:before-cell-row-fit'),
            _step('cell_row_fit_exact', 'mutate:cell-row-fit-exact', **fields),
            _where_step('where:after-cell-row-fit'),
        ),
        sources=(
            {
                'type': 'layout-mutation',
                'operation': op_summary,
                'risk_note': 'Accept only after rendered before/after proof on a disposable or working copy.',
            },
        ),
    )


def _build_table_column_width_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('table-column-width-exact', 'Apply guarded native pyhwpx.set_col_width to one exact pre-proven table.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Exact table target id/path from section-table-frame-inventory')
    parser.add_argument('--expected-hash', required=True, help='Exact proof_hash from section-table-frame-inventory')
    parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target before mutation')
    parser.add_argument('--expected-preimage-sha256', required=True, help='SHA-256 of the immutable candidate file opened for this disposable/working-copy operation')
    parser.add_argument('--expected-cell-inventory-hash', required=True, help='SHA-256 of the expected target cell-address/text inventory signature')
    parser.add_argument('--expected-document-text-hash', required=True, help='SHA-256 of flattened Hancom GetTextFile(UNICODE) text before mutation')
    parser.add_argument('--expected-text-char-count', type=int, required=True, help='Expected flattened document text character count')
    parser.add_argument('--expected-nonempty-line-count', type=int, required=True, help='Expected flattened document non-empty line count')
    parser.add_argument('--expected-div0-count', type=int, required=True, help='Expected literal #DIV/0! count in flattened document text')
    parser.add_argument('--expected-rows', type=int, required=True, help='Expected native table row count')
    parser.add_argument('--expected-cols', type=int, required=True, help='Expected native table column count')
    parser.add_argument('--expected-total-width-mm', type=float, required=True, help='Expected fixed native table total width in millimeters')
    parser.add_argument('--expected-table-height-mm', type=float, required=True, help='Expected native table height in millimeters before mutation')
    parser.add_argument('--expected-control-count', type=int, required=True, help='Expected bounded native control count before mutation')
    parser.add_argument('--expected-bindata-manifest-hash', required=True, help='SHA-256 of the expected BinData name/size/content manifest')
    parser.add_argument('--requested-widths-mm', type=float, nargs='+', required=True, help='One native column width in millimeters per expected column; total must equal expected total width')
    parser.add_argument('--confirm-layout', action='store_true', required=True, help='Required explicit confirmation for this native table-column layout mutation')
    args = _parse_bundle_args(parser, argv)

    def _sha256(value: str, *, field: str) -> str:
        raw = _bounded_text(value, field=field).lower()
        if raw.startswith('sha256:'):
            raw = raw[7:]
        if len(raw) != 64 or any(char not in '0123456789abcdef' for char in raw):
            raise BundleError(f'--{field.replace("_", "-")} must be a full 64-hex SHA-256 value, optionally prefixed with sha256:.')
        return f'sha256:{raw}'

    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    expected_preimage_sha256 = _sha256(args.expected_preimage_sha256, field='expected_preimage_sha256')
    expected_cell_inventory_hash = _sha256(args.expected_cell_inventory_hash, field='expected_cell_inventory_hash')
    expected_document_text_hash = _sha256(args.expected_document_text_hash, field='expected_document_text_hash')
    expected_bindata_manifest_hash = _sha256(args.expected_bindata_manifest_hash, field='expected_bindata_manifest_hash')
    expected_text_char_count = int(args.expected_text_char_count)
    expected_nonempty_line_count = int(args.expected_nonempty_line_count)
    expected_div0_count = int(args.expected_div0_count)
    expected_rows = int(args.expected_rows)
    expected_cols = int(args.expected_cols)
    expected_total_width_mm = float(args.expected_total_width_mm)
    expected_table_height_mm = float(args.expected_table_height_mm)
    expected_control_count = int(args.expected_control_count)
    widths = [float(value) for value in args.requested_widths_mm]
    if expected_text_char_count < 0 or expected_nonempty_line_count < 0 or expected_div0_count < 0:
        raise BundleError('Expected text counts must not be negative.')
    if not (1 <= expected_rows <= 200) or not (1 <= expected_cols <= 100):
        raise BundleError('Expected native table dimensions are outside the safe range.')
    if expected_control_count <= 0:
        raise BundleError('--expected-control-count must be positive.')
    for field, value in (
        ('expected_total_width_mm', expected_total_width_mm),
        ('expected_table_height_mm', expected_table_height_mm),
    ):
        if not math.isfinite(value) or not (0.1 <= value <= 500.0):
            raise BundleError(f'--{field.replace("_", "-")} is outside the safe finite range.')
    if len(widths) != expected_cols:
        raise BundleError(f'--requested-widths-mm must contain exactly {expected_cols} values.')
    if any(not math.isfinite(value) or not (0.1 <= value <= 200.0) for value in widths):
        raise BundleError('--requested-widths-mm contains a non-finite value or a value outside 0.1..200 mm.')
    if abs(sum(widths) - expected_total_width_mm) > 0.05:
        raise BundleError('--requested-widths-mm must sum to --expected-total-width-mm within 0.05 mm.')

    fields: dict[str, Any] = {
        'operation': 'native pyhwpx.set_col_width(widths_mm, as_=mm)',
        'target_id': target_id,
        'expected_hash': expected_hash,
        'expected_page': expected_page,
        'expected_preimage_sha256': expected_preimage_sha256,
        'expected_cell_inventory_hash': expected_cell_inventory_hash,
        'expected_document_text_hash': expected_document_text_hash,
        'expected_text_char_count': expected_text_char_count,
        'expected_nonempty_line_count': expected_nonempty_line_count,
        'expected_div0_count': expected_div0_count,
        'expected_rows': expected_rows,
        'expected_cols': expected_cols,
        'expected_total_width_mm': expected_total_width_mm,
        'expected_table_height_mm': expected_table_height_mm,
        'expected_control_count': expected_control_count,
        'expected_bindata_manifest_hash': expected_bindata_manifest_hash,
        'requested_widths_mm': widths,
        'confirm_layout': bool(args.confirm_layout),
        **scope,
    }
    if args.max_controls:
        fields['max_controls'] = int(args.max_controls)
    return BundleSpec(
        name='table-column-width-exact',
        summary='Apply one guarded native set_col_width call to an exact table with preimage, text, control, BinData, dimension, fixed-total, and rollback proof.',
        where=f'Table target {target_id!r} inside requested section/page scope, expected page {expected_page}; exact preimage and inventories are required.',
        how='Runs read-only where, validates the active working-copy SHA-256 and native text/control/BinData/table inventory, calls only documented pyhwpx.set_col_width(widths_mm, as_=mm), then verifies readback and restores original widths before failing if any preservation guard breaks.',
        changed='Mutates only the exact native table column widths after all guards pass; the command never saves the candidate and requires fresh Hancom rendered before/after proof.',
        steps=(
            _where_step('where:before-table-column-width'),
            _step('table_column_width_exact', 'mutate:table-column-width-exact', **fields),
            _where_step('where:after-table-column-width'),
        ),
        sources=(
            {
                'type': 'native-layout-mutation',
                'api': 'pyhwpx.set_col_width(*requested_widths_mm, as_=\'mm\')',
                'operation': 'requested_widths_mm=' + json.dumps(widths, ensure_ascii=False, separators=(',', ':')),
                'risk_note': 'Disposable/working-copy only; verify full-document Hancom render and package/text/control/BinData preservation before promotion.',
            },
        ),
    )


def _build_table_cell_structure_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('table-cell-structure-exact', 'Read-only exact table/cell structure probe before risky table-flow mutation.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Exact table target id/path from section-table-frame-inventory')
    parser.add_argument('--expected-hash', required=True, help='Exact proof_hash from section-table-frame-inventory')
    parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target before probing')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    fields: dict[str, Any] = {
        'target_id': target_id,
        'expected_hash': expected_hash,
        'expected_page': expected_page,
        **scope,
    }
    if args.max_controls:
        fields['max_controls'] = int(args.max_controls)
    return BundleSpec(
        name='table-cell-structure-exact',
        summary='Probe exactly one pre-proven table as read-only cell/container structure evidence; fail closed on target/hash/page/scope mismatch.',
        where=f'Table target {target_id!r} inside requested section/page scope, expected page {expected_page}.',
        how='Runs read-only where, then one `table_cell_structure_exact` primitive: re-inventory exact table, enter it through documented exact control selection + ShapeObjTextBoxEdit, read table/cell metrics, and test documented cell navigation without mutation.',
        changed='No document mutation. Returns single-cell/non-navigable evidence, co-anchored table/graphic fit-risk, and the next safe primitive/blocker hint before any table split or row-fit attempt.',
        steps=(
            _where_step('where:before-structure-probe'),
            _step('table_cell_structure_exact', 'probe:table-cell-structure-exact', **fields),
            _where_step('where:after-structure-probe'),
        ),
        sources=(
            {
                'type': 'doc-backed-read-only-probe',
                'pyhwpx_docs': [
                    'core.md: SelectCtrl(ctrllist, option=1) with Ctrl.GetCtrlInstID() for exact control proof',
                    'run.md: ShapeObjTextBoxEdit() enters selected table/textbox edit mode; selected table moves into A1',
                    'core.md: get_cell_addr/get_row_num/get_col_num/get_row_height/get_col_width/get_table_height/get_table_width/get_cell_margin read table/cell metrics',
                    'run.md: TableLeftCell/TableRightCell/TableUpperCell/TableLowerCell and core.md move_pos(100..107) for navigation probes',
                ],
                'risk_note': 'Read-only only. Do not promote TableSplitTable or row/cell mutation if navigation cannot leave A1.',
            },
        ),
    )


def _build_table_split_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('table-split-exact', 'Run documented Hancom TableSplitTable after exact table target proof.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Exact table target id/path from section-table-frame-inventory')
    parser.add_argument('--expected-hash', required=True, help='Exact proof_hash from section-table-frame-inventory')
    parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target before mutation')
    parser.add_argument('--down-rows', type=int, required=True, help='Move downward this many table cells/rows before running TableSplitTable; must be >= 1 because Hancom refuses first-row split')
    parser.add_argument('--confirm-layout', action='store_true', required=True, help='Required explicit confirmation for table split/page-break layout mutation')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    down_rows = int(args.down_rows)
    if not (1 <= down_rows <= 200):
        raise BundleError('--down-rows must be 1..200. Hancom Split Table cannot run from the first row.')
    fields: dict[str, Any] = {
        'target_id': target_id,
        'expected_hash': expected_hash,
        'expected_page': expected_page,
        'down_rows': down_rows,
        'confirm_layout': bool(args.confirm_layout),
        **scope,
    }
    if args.max_controls:
        fields['max_controls'] = int(args.max_controls)
    return BundleSpec(
        name='table-split-exact',
        summary='Split exactly one pre-proven table via documented Hancom TableSplitTable; fail closed on target/hash/page/scope or no-change proof.',
        where=f'Table target {target_id!r} inside requested section/page scope, expected page {expected_page}; split cursor after moving down {down_rows} row(s).',
        how='Runs read-only where, then one `table_split_exact` primitive: re-inventory exact table, enter its cell, move down by the explicit row count, and run documented `TableSplitTable`.',
        changed='Mutates table page-break structure only if target id, proof_hash, expected page, page/scope, cell-entry, downward navigation, and post-split control/metric change proof all succeed.',
        steps=(
            _where_step('where:before-table-split'),
            _step('table_split_exact', 'mutate:table-split-exact', **fields),
            _where_step('where:after-table-split'),
        ),
        sources=(
            {
                'type': 'documented-native-action',
                'hancom_help': 'Split Table splits beneath the row at the cursor position; it is recommended when no-split/inline overflow hides content.',
                'pyhwpx_action': 'hwp.Run("TableSplitTable") / HAction.Run("TableSplitTable")',
                'risk_note': 'Accept only after rendered before/after proof on a disposable or working copy.',
            },
        ),
    )


def _build_section_control_move_resize_exact(argv: Sequence[str]) -> BundleSpec:
    parser = _parser('section-control-move-resize-exact', 'Move and/or resize one control only after exact inventory proof matches.')
    _add_section_scope_args(parser)
    parser.add_argument('--target-id', required=True, help='Exact target id/path from section-control-inventory, e.g. ctrl/42/gso/no-inst')
    parser.add_argument('--expected-hash', required=True, help='Exact proof_hash from section-control-inventory')
    parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target before mutation')
    parser.add_argument('--scale-percent', type=float, help='Resize Width/Height by this percent, preserving aspect ratio')
    parser.add_argument('--move-dx-mm', type=float, default=0.0, help='Add this many mm to HorzOffset when the target exposes it')
    parser.add_argument('--move-dy-mm', type=float, default=0.0, help='Add this many mm to VertOffset when the target exposes it')
    parser.add_argument('--confirm-layout', action='store_true', required=True, help='Required explicit confirmation for one-control layout mutation')
    args = _parse_bundle_args(parser, argv)
    scope = _normalize_section_scope_args(args)
    target_id = _bounded_text(args.target_id, field='target_id')
    expected_hash = _bounded_text(args.expected_hash, field='expected_hash')
    expected_page = _positive_int(args.expected_page, field='expected_page')
    scale_percent = args.scale_percent
    move_dx_mm = float(args.move_dx_mm or 0.0)
    move_dy_mm = float(args.move_dy_mm or 0.0)
    if scale_percent is None and move_dx_mm == 0.0 and move_dy_mm == 0.0:
        raise BundleError('section-control-move-resize-exact requires --scale-percent or non-zero move delta.')
    if scale_percent is not None and not (5.0 <= float(scale_percent) <= 200.0):
        raise BundleError('--scale-percent must be between 5 and 200.')
    if abs(move_dx_mm) > 300.0 or abs(move_dy_mm) > 300.0:
        raise BundleError('--move-dx-mm/--move-dy-mm must be within +/-300mm.')
    fields: dict[str, Any] = {
        'target_id': target_id,
        'expected_hash': expected_hash,
        'expected_page': expected_page,
        'confirm_layout': bool(args.confirm_layout),
        **scope,
    }
    if scale_percent is not None:
        fields['scale_percent'] = float(scale_percent)
    if move_dx_mm != 0.0:
        fields['move_dx_mm'] = move_dx_mm
    if move_dy_mm != 0.0:
        fields['move_dy_mm'] = move_dy_mm
    op_bits = []
    if scale_percent is not None:
        op_bits.append(f'scale={float(scale_percent):g}%')
    if move_dx_mm:
        op_bits.append(f'dx={move_dx_mm:g}mm')
    if move_dy_mm:
        op_bits.append(f'dy={move_dy_mm:g}mm')
    return BundleSpec(
        name='section-control-move-resize-exact',
        summary='Move/resize exactly one pre-proven control; fail closed on target/hash/page/scope mismatch.',
        where=f'Target {target_id!r} inside the requested section/page scope, expected page {expected_page}.',
        how='Runs read-only where, then one `control_move_resize_exact` primitive that re-inventories controls and changes only Width/Height/HorzOffset/VertOffset on the exact matching control.',
        changed='Mutates one native control layout only if target id, proof_hash, expected page, and page/scope checks all match; server returns pre/post property proof.',
        steps=(
            _where_step('where:before-control-layout'),
            _step('control_move_resize_exact', 'mutate:move-resize-exact-control', **fields),
            _where_step('where:after-control-layout'),
        ),
        sources=(
            {
                'type': 'layout-mutation',
                'operation': ', '.join(op_bits),
                'risk_note': 'Accept only after rendered before/after proof on a disposable or working copy.',
            },
        ),
    )


_RECIPES: dict[str, BundleRecipe] = {
    'export-proof-range': BundleRecipe('export-proof-range', 'Export PDF for local proof-range rendering and manifest.', _build_export_proof_range),
    'where': BundleRecipe('where', 'Read-only current location proof.', _build_where),
    'context': BundleRecipe('context', 'Read-only structured edit-position context.', _build_context),
    'readback': BundleRecipe('readback', 'Read-only bounded LLM-friendly HWPX/Hancom readback.', _build_readback),
    'read-context': BundleRecipe('read-context', 'Alias for readback: bounded LLM-friendly current-context readback.', _build_read_context),
    'read-manifest': BundleRecipe('read-manifest', 'Read-only readback with raw artifact manifest path.', _build_read_manifest),
    'typography-overview': BundleRecipe('typography-overview', 'Read-only live Hancom/HWPML font-size/style overview.', _build_typography_overview),
    'text-table-to-native': BundleRecipe('text-table-to-native', 'Parse a pipe table and insert/fill native HWP table(s), optionally split by column; rendered proof required.', _build_text_table_to_native),
    'text-table-cleanup-selected': BundleRecipe('text-table-cleanup-selected', 'Delete exact active selected source text; separate rendered proof required.', _build_text_table_cleanup_selected),
    'table4-anchor-range-replace': BundleRecipe('table4-anchor-range-replace', 'Replace exact Table 4 start→before Figure 5 range with a native 4-column HWP table; rendered proof required.', _build_table4_anchor_range_replace),
    'selection-proof': BundleRecipe('selection-proof', 'Read-only active-selection proof with boundary risk flags.', _build_selection_proof),
    'selected-text-proof': BundleRecipe('selected-text-proof', 'Read-only selected-text proof plus location.', _build_selected_text_proof),
    'insert-text-file': BundleRecipe('insert-text-file', 'Insert UTF-8 text at an explicitly acknowledged active target.', _build_insert_text_file),
    'insert-before-anchor': BundleRecipe('insert-before-anchor', 'Insert text before an anchor with one live resolution.', _build_insert_before_anchor),
    'insert-after-anchor': BundleRecipe('insert-after-anchor', 'Insert text after an anchor with one live resolution.', _build_insert_after_anchor),
    'insert-after-paragraph': BundleRecipe('insert-after-paragraph', 'Insert text after an anchor paragraph with one live resolution.', _build_insert_after_paragraph),
    'insert-before-heading': BundleRecipe('insert-before-heading', 'Insert text before a heading with one live resolution.', _build_insert_before_heading),
    'cell-proof': BundleRecipe('cell-proof', 'Select/prove the table cell at the current caret position.', _build_cell_proof),
    'qa-profile': BundleRecipe('qa-profile', 'Export PDF for local section-scoped token/freshness QA.', _build_qa_profile),
    'section-control-inventory': BundleRecipe('section-control-inventory', 'Read-only section/page-scoped control inventory.', _build_section_control_inventory),
    'section-table-frame-inventory': BundleRecipe('section-table-frame-inventory', 'Read-only table/frame-flow inventory with co-anchored groups.', _build_section_table_frame_inventory),
    'section-frame-fill': BundleRecipe('section-frame-fill', 'Guarded dump-only frame-fill target proof; no mutation.', _build_section_frame_fill),
    'section-graphic-remove-or-hide': BundleRecipe('section-graphic-remove-or-hide', 'Guarded dump-only graphic remove/hide target proof; no mutation.', _build_section_graphic_remove_or_hide),
    'section-control-delete-exact': BundleRecipe('section-control-delete-exact', 'Delete one exact pre-proven control with fail-closed proof checks.', _build_section_control_delete_exact),
    'exact-control-select-proof': BundleRecipe('exact-control-select-proof', 'Read-only exact native control selection proof.', _build_exact_control_select_proof),
    'section-control-move-resize-exact': BundleRecipe('section-control-move-resize-exact', 'Move/resize one exact pre-proven control with fail-closed proof checks.', _build_section_control_move_resize_exact),
    'cell-format-exact': BundleRecipe('cell-format-exact', 'Apply first-pass exact native cell formatting with fail-closed proof checks.', _build_cell_format_exact),
    'cell-row-fit-exact': BundleRecipe('cell-row-fit-exact', 'Set one exact pre-proven table row height with fail-closed proof checks.', _build_cell_row_fit_exact),
    'table-cell-structure-exact': BundleRecipe('table-cell-structure-exact', 'Read-only exact table/cell structure and navigability probe.', _build_table_cell_structure_exact),
    'table-column-width-exact': BundleRecipe('table-column-width-exact', 'Apply guarded native exact table column widths with preservation and rollback proof.', _build_table_column_width_exact),
    'table-split-exact': BundleRecipe('table-split-exact', 'Split one exact pre-proven table with documented TableSplitTable.', _build_table_split_exact),
    'paragraph-style-apply-exact': BundleRecipe('paragraph-style-apply-exact', 'Apply exact paragraph style toggles with fail-closed match/page proof.', _build_paragraph_style_apply_exact),
    'paragraph-delete-exact': BundleRecipe('paragraph-delete-exact', 'Delete one exact paragraph/row after page and neighbor proof.', _build_paragraph_delete_exact),
    'style-apply': BundleRecipe('style-apply', 'Guarded dump-only style clone/apply proof; no mutation.', _build_style_apply),
    'style-clone': BundleRecipe('style-clone', 'Alias of guarded dump-only style apply proof; no mutation.', _build_style_clone),
    'style-inspect': BundleRecipe('style-inspect', 'Read-only native style inspection.', _build_style_inspect),
}


def iter_bundle_recipes() -> Iterable[BundleRecipe]:
    return (recipe for recipe in _RECIPES.values())


def recipe_names() -> list[str]:
    return sorted(_RECIPES)


def blocked_bundle_names() -> dict[str, str]:
    return dict(sorted(_BLOCKED_BUNDLE_NAMES.items()))


def build_named_bundle(name: str, argv: Sequence[str] | None = None) -> BundleSpec:
    bundle_name = name.strip()
    if bundle_name in _BLOCKED_BUNDLE_NAMES:
        raise BundleError(_BLOCKED_BUNDLE_NAMES[bundle_name])
    recipe = _RECIPES.get(bundle_name)
    if recipe is None:
        supported = ', '.join(recipe_names())
        raise BundleError(f'Unknown bundle {name!r}. Supported bundles: {supported}')
    return recipe.build(tuple(argv or ()))


def bundle_help(name: str) -> str:
    bundle_name = name.strip()
    if bundle_name in _BLOCKED_BUNDLE_NAMES:
        return f'{bundle_name}: blocked - 수정 안 됨 / no mutation performed - {_BLOCKED_BUNDLE_NAMES[bundle_name]}'
    if bundle_name not in _RECIPES:
        supported = ', '.join(recipe_names())
        raise BundleError(f'Unknown bundle {name!r}. Supported bundles: {supported}')
    if bundle_name == 'where':
        return 'usage: hwpx bundle-dump where\n       hwpx bundle-run where'
    if bundle_name == 'context':
        return 'usage: hwpx bundle-dump context\n       hwpx bundle-run context\n       hwpx context [--json]'
    if bundle_name == 'text-table-to-native':
        return 'usage: hwpx bundle-dump --with-meta text-table-to-native (--from-file PATH | --text TEXT) [--split-by-column HEADER] [--field-name NAME] --confirm-native-table\n       hwpx text-table-to-native (--from-file PATH | --text TEXT) [--split-by-column HEADER] --confirm-native-table\n       inserts one native HWP table, or one table per split group with the header row repeated; old source text is not deleted; rendered proof is required before cleanup/save'
    if bundle_name == 'text-table-cleanup-selected':
        return 'usage: hwpx bundle-dump --with-meta text-table-cleanup-selected (--from-file PATH | --text TEXT) [--expected-hash sha256:...] [--native-table-proof-ref REF] [--native-table-proof-hash HASH] --confirm-cleanup\n       hwpx text-table-cleanup-selected (--from-file PATH | --text TEXT) [--expected-hash sha256:...] --confirm-cleanup\n       deletes only the exact active selected source text outside table/cell context; separate from text-table-to-native; rendered before/after proof is required before save/final delivery'
    if bundle_name == 'table4-anchor-range-replace':
        return 'usage: hwpx bundle-dump --with-meta table4-anchor-range-replace --from-file PATH --section-anchor TEXT --start-anchor TEXT --end-before-anchor TEXT [--required-source-basename NAME] [--forbid-source-basename NAME] [--expected-range-hash sha256:...] --confirm-replace\n       hwpx table4-anchor-range-replace --from-file PATH --section-anchor TEXT --start-anchor TEXT --end-before-anchor TEXT --confirm-replace\n       selects/hashes the exact Table 4 range through immediately before Figure 5, deletes it, inserts a compact four-column native table, and requires rendered proof before save/final delivery'
    if bundle_name == 'selected-text-proof':
        return 'usage: hwpx bundle-dump selected-text-proof [--clear-selection]\n       hwpx bundle-run selected-text-proof [--clear-selection]'
    if bundle_name == 'insert-text-file':
        return 'usage: hwpx bundle-dump insert-text-file <text_file> --ack-active-target\n       hwpx bundle-run insert-text-file <text_file> --ack-active-target'
    if bundle_name == 'cell-proof':
        return 'usage: hwpx bundle-dump cell-proof\n       hwpx bundle-run cell-proof'
    if bundle_name == 'section-control-inventory':
        return 'usage: hwpx bundle-dump section-control-inventory (--section-anchor TEXT | --page-from N [--page-to N]) [--around TEXT]\n       hwpx bundle-run --json section-control-inventory ...'
    if bundle_name == 'section-table-frame-inventory':
        return 'usage: hwpx bundle-dump section-table-frame-inventory (--section-anchor TEXT | --page-from N [--page-to N]) [--around TEXT] [--target-id ID --expected-hash HASH --expected-page N]\n       read-only inventory of table/frame-flow controls, shape properties, and co-anchored graphics'
    if bundle_name == 'cell-row-fit-exact':
        return 'usage: hwpx bundle-dump cell-row-fit-exact (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N (--row-height-percent PCT | --row-height-hu HU | --row-height-mm MM | --resize-up-steps N | --resize-down-steps N | --line-spacing N | --char-height-percent PCT) --confirm-layout'
    if bundle_name == 'table-cell-structure-exact':
        return 'usage: hwpx bundle-dump table-cell-structure-exact (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N\n       read-only exact table/cell structure probe; reports single-cell/non-navigable evidence before any TableSplitTable or row-fit attempt'
    if bundle_name == 'table-split-exact':
        return 'usage: hwpx bundle-dump table-split-exact (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N --down-rows N --confirm-layout\n       splits one exact pre-proven table with documented TableSplitTable after explicit downward cell navigation'
    if bundle_name == 'export-proof-range':
        return 'usage: hwpx bundle-dump export-proof-range [--pages RANGE | --section-anchor TEXT [--until-anchor TEXT] | --all-pages] [--dpi N] [--out-dir DIR] [--anchor TOKEN]\n       hwpx export-proof-range (--pages RANGE | --section-anchor TEXT | --all-pages) --out-dir DIR [--fresh-session --source-hwp FILE]'
    if bundle_name == 'qa-profile':
        return 'usage: hwpx bundle-dump qa-profile [--section TEXT] [--forbid TOKEN] [--require TOKEN] [--after-anchor-forbid ANCHOR::TOKEN] [--source-hash SHA256]\n       hwpx qa-profile --out-dir DIR [same options]'
    if bundle_name == 'style-inspect':
        return 'usage: hwpx bundle-dump style-inspect [MATCH] [--keep-position]\n       hwpx style-inspect [MATCH] [--json]'
    if bundle_name == 'typography-overview':
        return 'usage: hwpx bundle-dump typography-overview [--max-samples N] [--max-sections N] [--max-styles N]\n       hwpx typography-overview [--json]\n       read-only live Hancom GetTextFile(HWPML2X) font/size/bold/style overview with a raw JSON artifact path'
    if bundle_name == 'paragraph-style-apply-exact':
        return 'usage: hwpx bundle-dump --with-meta paragraph-style-apply-exact --match TEXT --expected-page N (--keep-with-next on|off | --widow-orphan on|off | --pagebreak-before 0|1) [more style fields] --confirm-layout\n       hwpx paragraph-style-apply-exact --match TEXT --expected-page N [style fields] --confirm-layout'
    if bundle_name == 'paragraph-delete-exact':
        return 'usage: hwpx bundle-dump --with-meta paragraph-delete-exact --match TEXT --expected-page N [--occurrence-on-page N] [--expected-previous-contains TEXT] [--expected-next-contains TEXT] [--max-page-after N] --confirm-remove\n       deletes one exact paragraph/row after page and optional neighbor proof; rendered before/after proof is required before save/final delivery'
    if bundle_name in {'style-apply', 'style-clone'}:
        return f'usage: hwpx bundle-dump --with-meta {bundle_name} --from EXEMPLAR --to TARGET\n       guarded: read-only source/target style proof only; 수정 안 됨 / no mutation performed; live style mutation is not implemented'
    if bundle_name == 'section-frame-fill':
        return 'usage: hwpx bundle-dump --with-meta section-frame-fill (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --text-file body.txt --style-source ANCHOR [--expect-blank|--expect-token TOKEN]\n       guarded: read-only target proof only; 수정 안 됨 / no mutation performed; live fill is not implemented'
    if bundle_name == 'section-graphic-remove-or-hide':
        return 'usage: hwpx bundle-dump --with-meta section-graphic-remove-or-hide (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH [--expected-page N] (--confirm-remove | --hide-only)\n       guarded: read-only target proof only; 수정 안 됨 / no mutation performed; live remove/hide is not implemented'
    if bundle_name == 'section-control-delete-exact':
        return 'usage: hwpx bundle-dump --with-meta section-control-delete-exact (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N --confirm-remove\n       deletes one exact pre-proven control only after target/hash/page/scope proof matches'
    if bundle_name == 'exact-control-select-proof':
        return 'usage: hwpx bundle-dump --with-meta exact-control-select-proof (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N\n       selects/proves one exact pre-proven control, preferring CtrlInstID + SelectCtrl when available'
    if bundle_name == 'section-control-move-resize-exact':
        return 'usage: hwpx bundle-dump --with-meta section-control-move-resize-exact (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N [--scale-percent PCT] [--move-dx-mm MM] [--move-dy-mm MM] --confirm-layout\n       moves/resizes one exact pre-proven control only after target/hash/page/scope proof matches'
    if bundle_name == 'cell-format-exact':
        return 'usage: hwpx bundle-dump --with-meta cell-format-exact (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N (--cell-margin-hu HU | --cell-margin-mm MM | --vertical-align top|center|middle|bottom | --fill-color #RRGGBB | --border none) --confirm-layout\n       applies one exact pre-proven target-cell margin, alignment, fill, or border change only after target/hash/page/scope proof matches'
    if bundle_name == 'table-column-width-exact':
        return 'usage: hwpx bundle-dump --with-meta table-column-width-exact (--section-anchor TEXT | --page-from N [--page-to N]) --target-id ID --expected-hash HASH --expected-page N --expected-preimage-sha256 SHA256 --expected-cell-inventory-hash SHA256 --expected-document-text-hash SHA256 --expected-text-char-count N --expected-nonempty-line-count N --expected-div0-count N --expected-rows N --expected-cols N --expected-total-width-mm MM --expected-table-height-mm MM --expected-control-count N --expected-bindata-manifest-hash SHA256 --requested-widths-mm MM [MM ...] --confirm-layout\n       guarded native pyhwpx.set_col_width(widths_mm, as_=mm) with exact preimage, table, text, control, BinData, fixed-total, readback, and unsaved rollback proof'
    raise BundleError(f'No help registered for bundle {name!r}')


def dumps_server_payload(spec: BundleSpec) -> str:
    return json.dumps(spec.server_payload(), ensure_ascii=False, indent=2)


def dumps_debug_payload(spec: BundleSpec) -> str:
    return json.dumps(spec.debug_payload(), ensure_ascii=False, indent=2)
