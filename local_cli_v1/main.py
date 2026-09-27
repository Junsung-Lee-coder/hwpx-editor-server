from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .bundles import (
    BUNDLE_SERVER_OPS,
    BundleError,
    blocked_bundle_names,
    build_created_bundle,
    build_named_bundle,
    bundle_help,
    dumps_debug_payload,
    dumps_server_payload,
    iter_bundle_recipes,
)
from .envelope import build_envelope, dumps_envelope_json, format_human_envelope
from .output_parser import (
    dumps_normalized_json,
    format_command_bundle_human,
    format_context_human,
    format_readback_human,
    format_selection_proof_human,
    format_selected_text_proof_human,
    format_section_control_inventory_human,
    format_section_table_frame_inventory_human,
    format_style_inspect_human,
    format_table_cell_structure_human,
    format_typography_overview_human,
    format_where_bundle_human,
    normalize_command_bundle,
    summarize_context,
    summarize_readback,
    summarize_selection_proof,
    summarize_section_control_inventory,
    summarize_section_table_frame_inventory,
    summarize_style_inspect,
    summarize_table_cell_structure,
    summarize_typography_overview,
)
from .readback_diff import format_readback_diff_human, load_readback_manifest, summarize_readback_diff
from .gate_verdict import load_manifest as load_gate_manifest, summarize_gate_verdict
from .proof_packet import ProofPacketError, build_proof_packet, seal_native_border_readback
from .safe_schema import build_safe_agent_schema
from .static_inspector import (
    build_field_fill_plan,
    compare_static,
    inspect_static,
    load_replacements,
    output_format_policy,
    quick_render_static,
)
from .state import StatePersistenceError, clear_state, clear_session_binding, default_state_path, load_state, save_state, update_state
from .transport import ApiError, DEFAULT_BASE_URL, download_to_path, get_json, post_file, post_json
from app.config import get_settings
from app.poppler import PopplerResolutionError, resolve_pdftoppm


NO_MUTATION_TEXT = '수정 안 됨 / no mutation performed'


def _ensure_utf8_stdio() -> None:
    """Prefer UTF-8 CLI output so document text never crashes Windows consoles."""

    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


_ensure_utf8_stdio()


# `command-status` is derived from argparse + bundle registry.  These maps
# provide only per-command notes/classes that cannot be inferred from the
# parser or registry without calling the live Windows API.
LOCAL_METADATA_NOTES: dict[str, str] = {
    'help': 'local workflow guidance; no server call',
    'command-status': 'local parser + bundle registry migration metadata; no runtime probe',

    'safe-schema': 'local read-only safe operation schema for agents; exposes only read/render/info/planning/gate commands and denies mutation/XML/ZIP/BinData patch paths',
    'bundle-list': 'local bundle registry metadata; no server call',
    'bundles': 'alias of bundle-list; local bundle registry metadata; no server call',
    'bundle-help': 'local bundle registry help; no server call',
    'bundle-dump': 'builds strict command-bundle JSON without executing it',
    'tx-preview': 'writes a local transaction preview JSON with strict server_payload; no server call or mutation',
    'create-bundle': 'writes explicit command-bundle JSON without executing it',
    'bundle-create': 'alias of create-bundle; writes explicit command-bundle JSON without executing it',
    'bundle-compose': 'alias of create-bundle; writes explicit command-bundle JSON without executing it',
    'state': 'local cache inspection only; no server call or document mutation',
    'reset-state': 'local cache reset only; no server call or document mutation',
    'proof-packet': 'copies cached delivery/proof artifacts and writes hashes; no server call; no document mutation',
    'native-border-readback': 'seals candidate-bound native pre-quit and post-reopen border values into local evidence; no server call or document mutation',
    'readback-diff': 'local read-only source-vs-candidate readback manifest diff prioritized for font, size, native table, inside/outside-table, and control/image drift; bounded compact output with optional raw artifact',
    'static-info': 'local read-only secondary HWPX package/static inventory; no mutation; Hancom-native corroboration required before QA PASS',
    'static-read': 'local read-only secondary HWPX package/static text/table/header/footer/footnote/equation/image inventory; no mutation; Hancom-native corroboration required',
    'static-compare': 'local read-only secondary source-vs-candidate static comparison; no mutation; mismatch cannot PASS without Hancom-native evidence',
    'static-render': 'local read-only secondary quick HTML/SVG preview; non-authoritative; Hancom render remains required for QA PASS',
    'field-fill-plan': 'planning-only placeholder/field fill manifest; no mutation; production writes must use Hancom-native commands and proof',
    'output-format-policy': 'planning-only output extension/save/export policy manifest; no mutation; requires Hancom-native save/export proof',
    'gate-verdict': 'local read-only Hancom-primary gate verdict merger for hashes/tokens/counts/render proof/static supplements; no mutation and no external send',
}

DIRECT_ROUTE_NOTES: dict[str, str] = {
    'command-reconcile': 'durable timed-out native command reconciliation through the server route; no retry is admitted until terminal outcome',
    'open': 'lifecycle/session binding still uses direct route; original file remains untouched',
    'status': 'operator digest still uses direct status route plus local route probe',
    'session-health': 'runtime/route probe uses direct status/probe calls; not a document mutation',
    'bundle-health': 'alias of session-health; not a document mutation',
    'find': 'read-only match-state command still uses direct route; JSON, around-context, page-candidate, table/outside-table, and proof-match summaries are locally formatted',
    'info': 'match context command still uses direct route',
    'move': 'caret movement still uses direct route',
    'select': 'selection command still uses direct route',
    'cell': 'cell selection still uses direct route',
    'cellmove': 'cell movement still uses direct route',
    'cursormove': 'caret movement still uses direct route',
    'save': 'artifact/lifecycle command still uses direct route',
    'working-copy': 'artifact download still uses direct route',
    'undo': 'editor action still uses direct route',
    'redo': 'editor action still uses direct route',
    'table': 'layout-risk mutation still uses direct route',
    'list': 'layout-risk mutation still uses direct route',
    'screenshot': 'artifact capture still uses direct route/local render helper',
    'page-screenshot': 'artifact capture still uses direct export/render helper',
    'export': 'partial migration: `hwpx export --bundle-proof` uses the export_pdf command-bundle primitive; default legacy export remains direct-backlog',
    'close': 'lifecycle/session binding still uses direct route',
    'type': 'layout-risk mutation still uses direct route',
    'image': 'layout-risk mutation still uses direct route',
    'image-at-anchor': 'atomic live endpoint resolves one text anchor and inserts an image there; rendered proof required',
    'figure-section': 'atomic live endpoint uses one runtime lock for heading -> intro -> optional image -> caption -> body before target heading; rendered proof required',
    'cell-replace': 'table mutation still uses direct route; --cell is now current/anchor-table scoped and refuses global generated-field navigation; emits raw after_text artifact metadata (path, sha256, normalized hash, line count); rendered proof is still required',
    'cell-replace-exact': 'alias of cell-replace with exact cell/page guards; --cell stays inside the current/anchor table and does not use global duplicate A1-style field names; rendered proof is required',
    'fontsize': 'character-shape mutation still uses direct route',
    'bold': 'selection-required character-shape mutation still uses direct route; fails closed without selected-text proof',
    'font': 'character-shape mutation still uses direct route',
    'bullet': 'list mutation still uses direct route',
    'pycall': 'controlled macro escape hatch; run rendered proof after stateful calls',
    'action': 'controlled HAction escape hatch; run rendered proof after stateful actions',
}

DISABLED_COMMAND_NOTES: dict[str, str] = {
    'replace': f'atomic replace is reserved until Hancom-native proof exists; {NO_MUTATION_TEXT}',
    'section-frame-fill': f'guarded dump-only target proof; {NO_MUTATION_TEXT}',
    'section-graphic-remove-or-hide': f'guarded dump-only target proof; {NO_MUTATION_TEXT}',
    'style-apply': f'guarded dump-only style proof; {NO_MUTATION_TEXT}',
    'style-clone': f'guarded dump-only style proof; {NO_MUTATION_TEXT}',
}

BUNDLE_COMMAND_NOTES: dict[str, str] = {
    'where': 'local bundle recipe exists; running route availability is runtime: unknown until status/session-health probe',
    'readback': 'LLM-friendly read-only HWPX/Hancom readback for caret/selection/page/document scopes; compact output plus raw artifact path; runtime route availability unknown until status/session-health probe',
    'read-context': 'alias of readback; LLM-friendly read-only HWPX/Hancom context summary with bounded JSON/human output',
    'read-manifest': 'read-only readback defaulting to document scope; bounded/compact summary includes raw artifact manifest path for full evidence',
    'typography-overview': 'read-only live Hancom/HWPML2X typography overview for active document; returns font/size/bold/style-variant counts plus raw artifact path',
    'bundle-run': 'posts a named local bundle to /local-cli/command-bundle; runtime route availability unknown here',
    'bundle': 'posts explicit bundle JSON to /local-cli/command-bundle; runtime route availability unknown here',
    'export-proof-range': 'export_pdf bundle plus local rendered proof manifest; rendered artifacts are proof standard',
    'qa-profile': 'export_pdf bundle plus local token/freshness QA manifest',
    'paragraph-style-apply-exact': 'bundle-backed paragraph style mutation; no auto-save; rendered proof required before save',
    'paragraph-delete-exact': 'bundle-backed guarded paragraph removal; page/neighbor guards and rendered proof required before save',
    'text-table-to-native': 'bundle-backed native table insertion from a parsed pipe table, with optional split-by-column planning; no source text deletion; rendered proof required before cleanup/save',
    'text-table-cleanup-selected': 'bundle-backed cleanup mutation; deletes only exact active selected source text outside table/cell context; rendered before/after proof required before save',
    'table4-anchor-range-replace': 'bundle-backed guarded Table 4 range replacement; selects/hashes start→before Figure 5, deletes old source text, inserts native 4-column table; rendered proof required before save',
    'tx-commit': 'posts the exact server_payload from a tx-preview file to /local-cli/command-bundle; no auto-save',
    'insert-before-anchor': 'safe anchor_insert primitive; one live anchor resolution and caret progression; rendered proof required before save',
    'insert-after-anchor': 'safe anchor_insert primitive; one live anchor resolution and caret progression; rendered proof required before save',
    'insert-after-paragraph': 'safe anchor_insert primitive; moves to anchor paragraph end once and inserts with leading newline; rendered proof required before save',
    'insert-before-heading': 'safe heading insertion primitive; one live anchor resolution and caret progression; rendered proof required before save',
    'figure-section': 'atomic live figure-section bundle: heading -> intro -> optional image -> caption -> body before next heading',
}


ALLOWED_COMMAND_STATUSES = {'bundle-backed', 'direct-backlog', 'disabled'}


def _parser_command_names(parser: argparse.ArgumentParser | None = None) -> set[str]:
    parser = parser or build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise ApiError('parser has no subparsers')


def build_command_status(parser: argparse.ArgumentParser | None = None) -> dict[str, dict[str, str]]:
    """Build local command migration metadata from parser + bundle registry.

    This deliberately does not call the live Windows API. Runtime route truth is
    reported as unknown here; use `hwpx status` or `hwpx session-health` for the
    non-mutating runtime probe.
    """

    parser_names = _parser_command_names(parser)
    recipe_by_name = {recipe.name: recipe for recipe in iter_bundle_recipes()}
    blocked_by_name = blocked_bundle_names()
    names = set(parser_names) | set(recipe_by_name) | set(blocked_by_name)
    status: dict[str, dict[str, str]] = {}

    for name in sorted(names):
        if name in DISABLED_COMMAND_NOTES:
            status[name] = {'status': 'disabled', 'note': DISABLED_COMMAND_NOTES[name], 'source': 'parser+guarded-bundle'}
        elif name in blocked_by_name:
            status[name] = {
                'status': 'disabled',
                'note': f'blocked bundle recipe; {NO_MUTATION_TEXT}; {blocked_by_name[name]}',
                'source': 'blocked-bundle-registry',
            }
        elif name in LOCAL_METADATA_NOTES:
            status[name] = {'status': 'bundle-backed', 'note': LOCAL_METADATA_NOTES[name], 'source': 'parser-local'}
        elif name in recipe_by_name or name in {'bundle-run', 'bundle'}:
            recipe = recipe_by_name.get(name)
            note = BUNDLE_COMMAND_NOTES.get(name)
            if note is None and recipe is not None:
                note = f'{recipe.summary}; runtime route availability is unknown until status/session-health probe'
            elif note is None:
                note = 'command-bundle execution path; runtime route availability is unknown until status/session-health probe'
            source = 'parser+bundle-registry' if name in parser_names and name in recipe_by_name else 'bundle-registry'
            status[name] = {'status': 'bundle-backed', 'note': note, 'source': source}
        else:
            note = DIRECT_ROUTE_NOTES.get(name)
            if note is None:
                note = 'parser command not in bundle registry yet; treat as direct-route migration backlog until proven otherwise'
            status[name] = {'status': 'direct-backlog', 'note': note, 'source': 'parser-direct'}

    missing_parser = parser_names - set(status)
    invalid = {name: meta.get('status') for name, meta in status.items() if meta.get('status') not in ALLOWED_COMMAND_STATUSES}
    if missing_parser or invalid:
        raise ApiError(f'Invalid derived command-status metadata: missing_parser={sorted(missing_parser)} invalid={invalid!r}')
    return status


def command_status_names() -> tuple[str, ...]:
    return tuple(build_command_status())


SAFE_WORKFLOW_TEXT = '''standard safe edit+proof workflow:
1. status: confirm runtime, active document, dirty state, working copy, last proof, and next action.
2. open: open one local HWP/HWPX into a server-managed working copy.
3. find/where/select: find the target, prove current location/selection, and confirm the intended working copy.
4. edit: prefer bundle-backed commands/recipes with explicit where/how/changed proof; use anchor commands (`insert-before-heading`, `figure-section`) instead of repeated insertion before one anchor.
5. screenshot/export-proof: capture rendered proof with screenshot --mode page, page-screenshot, export, or export-proof-range.
6. save: save/download the working copy only after rendered proof is acceptable.
7. close: close the live session after saving/reporting artifacts.

review rule: page count is metadata only, not validation proof. Trust rendered PDF/page PNG/live Hancom artifacts and visible target/content review.
command states: run `hwpx command-status` for bundle-backed / direct-backlog / disabled status.
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='hwpx', description='Fresh thin HWPX local CLI v1')
    parser.add_argument(
        '--base-url',
        default=None,
        help='HWPX server base URL (defaults to cached session base URL, then HWPX_BASE_URL, then http://127.0.0.1:8765)',
    )

    subparsers = parser.add_subparsers(dest='command', required=True)

    help_parser = subparsers.add_parser('help', help='Show HWPX safe workflow help')
    help_parser.add_argument('topic', nargs='?', choices=('workflow', 'commands'), default='workflow')
    safe_schema_parser = subparsers.add_parser('safe-schema', help='Show non-mutating agent-safe command schema')
    safe_schema_parser.add_argument('--json', action='store_true', help='Print safe schema JSON')
    subparsers.add_parser('command-status', help='Show command states: bundle-backed / direct-backlog / disabled')
    command_reconcile_parser = subparsers.add_parser(
        'command-reconcile',
        help='Reconcile one durable timed-out native command before retrying or closing a session',
    )
    command_reconcile_parser.add_argument('--command-id', required=True)
    command_reconcile_parser.add_argument('--session-id', default=None)
    command_reconcile_parser.add_argument('--json', action='store_true', help='Print reconciliation JSON')

    open_parser = subparsers.add_parser('open', help='Open a local HWP/HWPX file into a live server session')
    open_parser.add_argument('file', type=Path)
    open_parser.add_argument('--json', action='store_true', help='Print the structured lifecycle response')

    status_parser = subparsers.add_parser('status', help='Show thin runtime status')
    status_parser.add_argument('--json', action='store_true', help='Print the structured runtime status JSON')
    subparsers.add_parser('session-health', aliases=['bundle-health'], help='Check cached session and command-bundle route health')
    subparsers.add_parser('state', help='Show cached local session and artifact state')
    proof_packet_parser = subparsers.add_parser('proof-packet', help='Collect cached proof artifacts without a server call or document mutation')
    proof_packet_parser.add_argument('--out-dir', type=Path, required=True, help='Destination directory for the proof packet')
    proof_packet_parser.add_argument('--json', action='store_true', help='Print the packet manifest as JSON')
    native_border_parser = subparsers.add_parser(
        'native-border-readback',
        help='Seal candidate-bound native border values read before save/quit and after reopen',
    )
    native_border_parser.add_argument('--pre-quit', type=Path, required=True, help='JSON object containing the native value readback before save/quit')
    native_border_parser.add_argument('--persisted', type=Path, required=True, help='JSON object containing the native value readback after reopening the saved document')
    native_border_parser.add_argument('--target-identity', type=Path, required=True, help='JSON object identifying the selected native target')
    native_border_parser.add_argument('--out', type=Path, required=True, help='Exact output path for the candidate-bound readback artifact')
    native_border_parser.add_argument('--json', action='store_true', help='Print the sealed artifact envelope as JSON')
    subparsers.add_parser('reset-state', help='Clear the cached local CLI state file without calling the server')

    find_parser = subparsers.add_parser('find', help='Find text in the active working copy')
    find_parser.add_argument('text')
    find_parser.add_argument('--json', action='store_true', help='Print structured find JSON')
    find_parser.add_argument('--with-page', action='store_true', help='Include page-candidate fields and approximation warnings')
    find_parser.add_argument('--around', type=int, default=0, choices=range(0, 6), help='Include N before/after text blocks for each match')
    find_parser.add_argument('--proof-match', type=int, help='Return one read-only match proof by 1-based match index')
    find_parser.add_argument('--proof-out-dir', type=Path, help='Directory for rendered proof pages; defaults next to the source')
    find_parser.add_argument('--dpi', type=int, default=160, help='Render DPI for --proof-match')
    find_parser.add_argument('--contact-sheet', action='store_true', help='Create a proof contact sheet when rendering --proof-match')

    info_parser = subparsers.add_parser('info', help='Show nearby context for a match')
    info_parser.add_argument('target')

    move_parser = subparsers.add_parser('move', help='Jump to the start of a requested match')
    move_parser.add_argument('target')

    select_parser = subparsers.add_parser('select', help='Select a direct content match or numbered cached match')
    select_parser.add_argument('target')

    subparsers.add_parser('cell', help='Select the table cell at the current caret position')

    cellmove_parser = subparsers.add_parser('cellmove', help='Move the current cell selection')
    cellmove_parser.add_argument('direction')
    cellmove_parser.add_argument('count', type=int)

    cursormove_parser = subparsers.add_parser('cursormove', help='Move the caret')
    cursormove_parser.add_argument('direction')
    cursormove_parser.add_argument('count', type=int)

    where_parser = subparsers.add_parser('where', help='Show a bundle-backed current document/location summary')
    where_parser.add_argument('--json', action='store_true', help='Print normalized command-bundle parser JSON')

    context_parser = subparsers.add_parser('context', help='Show a structured current edit-position context card')
    context_parser.add_argument('--json', action='store_true', help='Print structured context JSON')

    readback_parser = subparsers.add_parser('readback', aliases=['read-context'], help='Show bounded LLM-friendly HWPX/Hancom readback for caret/selection/page/document scopes')
    readback_parser.add_argument('--scope', choices=('caret', 'selection', 'page', 'document'), default='caret')
    readback_parser.add_argument('--page-from', type=int, help='Optional first 1-based page for page/range readback')
    readback_parser.add_argument('--page-to', type=int, help='Optional last 1-based page for page/range readback')
    readback_parser.add_argument('--max-blocks', type=int, default=300, help='Maximum outside-text blocks in compact JSON')
    readback_parser.add_argument('--max-table-cells', type=int, default=800, help='Maximum table cells in compact JSON')
    readback_parser.add_argument('--max-controls', type=int, default=2048, help='Maximum controls/images/tables in compact JSON')
    readback_parser.add_argument('--json', action='store_true', help='Print compact readback JSON')

    read_manifest_parser = subparsers.add_parser('read-manifest', help='Show document-scope readback and raw artifact manifest path')
    read_manifest_parser.add_argument('--scope', choices=('caret', 'selection', 'page', 'document'), default='document')
    read_manifest_parser.add_argument('--page-from', type=int, help='Optional first 1-based page for page/range readback')
    read_manifest_parser.add_argument('--page-to', type=int, help='Optional last 1-based page for page/range readback')
    read_manifest_parser.add_argument('--max-blocks', type=int, default=300, help='Maximum outside-text blocks in compact JSON')
    read_manifest_parser.add_argument('--max-table-cells', type=int, default=800, help='Maximum table cells in compact JSON')
    read_manifest_parser.add_argument('--max-controls', type=int, default=2048, help='Maximum controls/images/tables in compact JSON')
    read_manifest_parser.add_argument('--json', action='store_true', help='Print compact readback JSON')

    typography_parser = subparsers.add_parser('typography-overview', help='Show live Hancom/HWPML font, size, bold, and style-variant overview')
    typography_parser.add_argument('--scope', choices=('document',), default='document')
    typography_parser.add_argument('--max-samples', type=int, default=40, help='Maximum sample bucket budget')
    typography_parser.add_argument('--max-sections', type=int, default=30, help='Maximum section overview entries')
    typography_parser.add_argument('--max-styles', type=int, default=40, help='Maximum font/size/style rows')
    typography_parser.add_argument('--json', action='store_true', help='Print compact typography overview JSON')

    readback_diff_parser = subparsers.add_parser(
        'readback-diff',
        help='Compare source-vs-candidate readback/read-manifest JSON files for font, size, native-table, and control/image drift',
        description='Compare source-vs-candidate readback/read-manifest JSON files for font, size, native-table, inside/outside-table, and control/image drift.',
    )
    readback_diff_parser.add_argument('source_manifest', type=Path, help='Source/baseline readback JSON manifest')
    readback_diff_parser.add_argument('candidate_manifest', type=Path, help='Candidate readback JSON manifest')
    readback_diff_parser.add_argument('--artifact-dir', type=Path, help='Directory for full raw diff artifact when compact output is capped')
    readback_diff_parser.add_argument('--max-issues', type=int, default=20, help='Maximum issues to include in compact JSON/human output')
    readback_diff_parser.add_argument('--json', action='store_true', help='Print compact readback-diff JSON')

    for command_name, help_text in (
        ('static-info', 'Read-only secondary static HWPX package inventory; Hancom-native corroboration required'),
        ('static-read', 'Read-only secondary static HWPX text/table/header/footer/footnote/equation/image inventory'),
    ):
        static_parser = subparsers.add_parser(command_name, help=help_text)
        static_parser.add_argument('file', type=Path)
        static_parser.add_argument('--engine', choices=('auto', 'hwpx-package-xml', 'rhwp'), default='auto')
        static_parser.add_argument('--artifact-dir', type=Path, help='Optional artifact directory for raw manifest/extracted read-only files')
        static_parser.add_argument('--extract-images', action='store_true', help='Extract BinData/image entries read-only under --artifact-dir')
        static_parser.add_argument('--json', action='store_true')

    static_compare_parser = subparsers.add_parser('static-compare', help='Read-only secondary source-vs-candidate static comparison')
    static_compare_parser.add_argument('source_file', type=Path)
    static_compare_parser.add_argument('candidate_file', type=Path)
    static_compare_parser.add_argument('--artifact-dir', type=Path)
    static_compare_parser.add_argument('--json', action='store_true')

    static_render_parser = subparsers.add_parser('static-render', help='Read-only non-authoritative quick HTML/SVG static preview')
    static_render_parser.add_argument('file', type=Path)
    static_render_parser.add_argument('--format', choices=('html', 'svg'), default='html')
    static_render_parser.add_argument('--output', type=Path, required=True)
    static_render_parser.add_argument('--artifact-dir', type=Path)
    static_render_parser.add_argument('--json', action='store_true')

    field_plan_parser = subparsers.add_parser('field-fill-plan', help='Plan placeholder/field filling only; no mutation; Hancom-native write required')
    field_plan_parser.add_argument('file', type=Path)
    field_plan_parser.add_argument('replacements_json', type=Path)
    field_plan_parser.add_argument('--artifact-dir', type=Path)
    field_plan_parser.add_argument('--json', action='store_true')

    format_policy_parser = subparsers.add_parser('output-format-policy', help='Read-only output extension/save/export policy manifest')
    format_policy_parser.add_argument('input_file', type=Path)
    format_policy_parser.add_argument('output_file', type=Path)
    format_policy_parser.add_argument('--operation', choices=('save', 'save-as', 'export'), default='save')
    format_policy_parser.add_argument('--json', action='store_true')

    gate_parser = subparsers.add_parser('gate-verdict', help='Read-only Hancom-primary gate verdict from manifests, token gates, counts, render proof')
    gate_parser.add_argument('source_manifest', type=Path)
    gate_parser.add_argument('candidate_manifest', type=Path)
    gate_parser.add_argument('--planned-mutations', type=int, default=0)
    gate_parser.add_argument('--require', action='append', default=[])
    gate_parser.add_argument('--forbid', action='append', default=[])
    gate_parser.add_argument('--render-manifest', type=Path)
    gate_parser.add_argument('--static-supplement', type=Path)
    gate_parser.add_argument('--allow-count-drift', action='append', default=[])
    gate_parser.add_argument('--json', action='store_true')

    selection_proof_parser = subparsers.add_parser(
        'selection-proof',
        help='Show robust read-only proof of the active selection, boundaries, and risk flags',
    )
    selection_proof_parser.add_argument('--json', action='store_true', help='Print structured selection-proof JSON')

    selected_text_proof_parser = subparsers.add_parser(
        'selected-text-proof',
        help='Show a bundle-backed proof of the active selected text',
    )
    selected_text_proof_parser.add_argument(
        '--clear-selection',
        action='store_true',
        help='Ask the server not to keep the current selection after reading selected text.',
    )
    selected_text_proof_parser.add_argument('--json', action='store_true', help='Print normalized command-bundle parser JSON')

    section_inventory_parser = subparsers.add_parser(
        'section-control-inventory',
        help='List read-only controls/images/tables/frames around a section anchor or page range',
    )
    section_inventory_scope = section_inventory_parser.add_mutually_exclusive_group(required=True)
    section_inventory_scope.add_argument('--section-anchor', help='Section heading/anchor text')
    section_inventory_scope.add_argument('--page-from', type=int, help='First 1-based page to include')
    section_inventory_parser.add_argument('--page-to', type=int, help='Last 1-based page to include')
    section_inventory_parser.add_argument('--around', help='Optional nearby anchor/token')
    section_inventory_parser.add_argument('--max-controls', type=int, default=2048, help='Maximum controls to enumerate')
    section_inventory_parser.add_argument('--json', action='store_true', help='Print normalized inventory JSON')

    table_frame_inventory_parser = subparsers.add_parser(
        'section-table-frame-inventory',
        help='List read-only table/frame-flow controls with co-anchored graphics and shape properties',
    )
    table_frame_inventory_scope = table_frame_inventory_parser.add_mutually_exclusive_group(required=True)
    table_frame_inventory_scope.add_argument('--section-anchor', help='Section heading/anchor text')
    table_frame_inventory_scope.add_argument('--page-from', type=int, help='First 1-based page to include')
    table_frame_inventory_parser.add_argument('--page-to', type=int, help='Last 1-based page to include')
    table_frame_inventory_parser.add_argument('--around', help='Optional nearby anchor/token')
    table_frame_inventory_parser.add_argument('--target-id', help='Optional exact target id/path from section-control-inventory')
    table_frame_inventory_parser.add_argument('--expected-hash', help='Optional exact target proof hash')
    table_frame_inventory_parser.add_argument('--expected-page', type=int, help='Optional expected target page')
    table_frame_inventory_parser.add_argument('--max-controls', type=int, default=2048, help='Maximum controls to enumerate')
    table_frame_inventory_parser.add_argument('--json', action='store_true', help='Print normalized inventory JSON')

    table_cell_structure_parser = subparsers.add_parser(
        'table-cell-structure-exact',
        help='Read-only exact table/cell structure and navigability probe before table-flow mutation',
    )
    table_cell_structure_scope = table_cell_structure_parser.add_mutually_exclusive_group(required=True)
    table_cell_structure_scope.add_argument('--section-anchor', help='Section heading/anchor text')
    table_cell_structure_scope.add_argument('--page-from', type=int, help='First 1-based page to include')
    table_cell_structure_parser.add_argument('--page-to', type=int, help='Last 1-based page to include')
    table_cell_structure_parser.add_argument('--around', help='Optional nearby anchor/token')
    table_cell_structure_parser.add_argument('--target-id', required=True, help='Exact table target id/path from section-table-frame-inventory')
    table_cell_structure_parser.add_argument('--expected-hash', required=True, help='Exact target proof hash')
    table_cell_structure_parser.add_argument('--expected-page', type=int, required=True, help='Expected target page')
    table_cell_structure_parser.add_argument('--max-controls', type=int, default=2048, help='Maximum controls to enumerate')
    table_cell_structure_parser.add_argument('--json', action='store_true', help='Print normalized structure JSON')

    section_frame_fill_parser = subparsers.add_parser(
        'section-frame-fill',
        help='Guarded dump-only frame-fill target proof; live mutation is blocked',
    )
    section_frame_fill_parser.add_argument('--dump-spec', action='store_true', required=True, help='Required: print guarded bundle spec only')
    section_frame_scope = section_frame_fill_parser.add_mutually_exclusive_group(required=True)
    section_frame_scope.add_argument('--section-anchor')
    section_frame_scope.add_argument('--page-from', type=int)
    section_frame_fill_parser.add_argument('--page-to', type=int)
    section_frame_fill_parser.add_argument('--around')
    section_frame_fill_parser.add_argument('--target-id', required=True)
    section_frame_fill_parser.add_argument('--text-file', type=Path, required=True)
    section_frame_fill_parser.add_argument('--style-source', '--style-recipe', dest='style_source', required=True)
    section_frame_fill_parser.add_argument('--expect-blank', action='store_true')
    section_frame_fill_parser.add_argument('--expect-token')
    section_frame_fill_parser.add_argument('--max-controls', type=int, default=2048)

    graphic_parser = subparsers.add_parser(
        'section-graphic-remove-or-hide',
        help='Guarded dump-only graphic/control target proof; live remove/hide is blocked',
    )
    graphic_parser.add_argument('--dump-spec', action='store_true', required=True, help='Required: print guarded bundle spec only')
    graphic_scope = graphic_parser.add_mutually_exclusive_group(required=True)
    graphic_scope.add_argument('--section-anchor')
    graphic_scope.add_argument('--page-from', type=int)
    graphic_parser.add_argument('--page-to', type=int)
    graphic_parser.add_argument('--around')
    graphic_parser.add_argument('--target-id', required=True)
    graphic_parser.add_argument('--expected-hash', required=True)
    graphic_parser.add_argument('--expected-page', type=int)
    graphic_mode = graphic_parser.add_mutually_exclusive_group(required=True)
    graphic_mode.add_argument('--confirm-remove', action='store_true')
    graphic_mode.add_argument('--hide-only', action='store_true')
    graphic_parser.add_argument('--max-controls', type=int, default=2048)

    exact_delete_parser = subparsers.add_parser(
        'section-control-delete-exact',
        help='Delete one exact pre-proven control and write before/after rendered proof manifest',
    )
    exact_delete_scope = exact_delete_parser.add_mutually_exclusive_group(required=True)
    exact_delete_scope.add_argument('--section-anchor')
    exact_delete_scope.add_argument('--page-from', type=int)
    exact_delete_parser.add_argument('--page-to', type=int)
    exact_delete_parser.add_argument('--around')
    exact_delete_parser.add_argument('--target-id', required=True)
    exact_delete_parser.add_argument('--expected-hash', required=True)
    exact_delete_parser.add_argument('--expected-page', type=int, required=True)
    exact_delete_parser.add_argument('--confirm-remove', action='store_true', required=True)
    exact_delete_parser.add_argument('--max-controls', type=int, default=2048)
    exact_delete_parser.add_argument('--proof-out-dir', type=Path, required=True, help='Output directory for before/after render proof and manifest')
    exact_delete_parser.add_argument('--dpi', type=int, default=160)
    exact_delete_parser.add_argument('--json', action='store_true')

    exact_layout_parser = subparsers.add_parser(
        'section-control-move-resize-exact',
        help='Move/resize one exact pre-proven control and write before/after rendered proof manifest',
    )
    exact_layout_scope = exact_layout_parser.add_mutually_exclusive_group(required=True)
    exact_layout_scope.add_argument('--section-anchor')
    exact_layout_scope.add_argument('--page-from', type=int)
    exact_layout_parser.add_argument('--page-to', type=int)
    exact_layout_parser.add_argument('--around')
    exact_layout_parser.add_argument('--target-id', required=True)
    exact_layout_parser.add_argument('--expected-hash', required=True)
    exact_layout_parser.add_argument('--expected-page', type=int, required=True)
    exact_layout_parser.add_argument('--scale-percent', type=float, help='Resize Width/Height by this percent, preserving aspect ratio')
    exact_layout_parser.add_argument('--move-dx-mm', type=float, default=0.0)
    exact_layout_parser.add_argument('--move-dy-mm', type=float, default=0.0)
    exact_layout_parser.add_argument('--confirm-layout', action='store_true', required=True)
    exact_layout_parser.add_argument('--max-controls', type=int, default=2048)
    exact_layout_parser.add_argument('--proof-out-dir', type=Path, required=True, help='Output directory for before/after render proof and manifest')
    exact_layout_parser.add_argument('--dpi', type=int, default=160)
    exact_layout_parser.add_argument('--json', action='store_true')

    export_proof_parser = subparsers.add_parser(
        'export-proof-range',
        help='Bundle-backed PDF export, local page rendering, and manifest JSON for a page range',
    )
    export_proof_parser.add_argument('--pages', help='Page range expression, e.g. 19-25 or 19-21,23')
    export_proof_parser.add_argument('--all-pages', action='store_true', help='Render every page in the freshly exported PDF')
    export_proof_parser.add_argument('--dpi', type=int, default=160, help='Render DPI')
    export_proof_parser.add_argument('--out-dir', type=Path, required=True, help='Output directory for rendered proofs and manifest')
    export_proof_parser.add_argument('--anchor', action='append', default=[], help='Token to verify in extracted PDF text; repeatable')
    export_proof_parser.add_argument('--section-anchor', help='Derive proof page range from the first PDF-text page containing this anchor')
    export_proof_parser.add_argument('--until-anchor', help='End derived section proof before the first later PDF-text page containing this anchor')
    export_proof_parser.add_argument('--fresh-session', action='store_true', help='Reopen a saved HWP/HWPX path before exporting')
    export_proof_parser.add_argument('--source-hwp', type=Path, help='Saved HWP/HWPX path to reopen for --fresh-session (defaults to cached source path)')
    export_proof_parser.add_argument('--contact-sheet', action='store_true', help='Create a contact sheet when local ImageMagick montage is available')
    export_proof_parser.add_argument('--json', action='store_true', help='Print manifest JSON path and payload')

    style_inspect_parser = subparsers.add_parser('style-inspect', help='Bundle-backed read-only style inspection at a match or current caret')
    style_inspect_parser.add_argument('match', nargs='?', help='Optional text match/anchor to inspect')
    style_inspect_parser.add_argument('--keep-position', action='store_true', help='Do not restore caret after inspecting a match')
    style_inspect_parser.add_argument('--json', action='store_true', help='Print normalized style JSON')

    paragraph_style_parser = subparsers.add_parser(
        'paragraph-style-apply-exact',
        help='Bundle-backed exact paragraph style mutation; requires match, expected page, and layout confirmation',
    )
    paragraph_style_parser.add_argument('--match', required=True, help='Unique paragraph text/anchor to style')
    paragraph_style_parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target')
    paragraph_style_parser.add_argument('--keep-with-next', choices=('on', 'off'), help='Set native KeepWithNext paragraph flag')
    paragraph_style_parser.add_argument('--widow-orphan', choices=('on', 'off'), help='Set native WidowOrphan paragraph flag')
    paragraph_style_parser.add_argument('--pagebreak-before', type=int, choices=(0, 1), help='Set native PagebreakBefore paragraph flag to 0 or 1')
    paragraph_style_parser.add_argument('--confirm-layout', action='store_true', required=True, help='Required explicit confirmation for paragraph layout/style mutation')
    paragraph_style_parser.add_argument('--json', action='store_true', help='Print normalized command-bundle parser JSON')

    paragraph_delete_parser = subparsers.add_parser(
        'paragraph-delete-exact',
        help='Bundle-backed exact paragraph/scaffold-row cleanup; requires match, expected page, and remove confirmation',
    )
    paragraph_delete_parser.add_argument('--match', required=True, help='Exact text/anchor in the paragraph to remove')
    paragraph_delete_parser.add_argument('--expected-page', type=int, required=True, help='Expected rendered/page evidence for the target')
    paragraph_delete_parser.add_argument('--occurrence-on-page', type=int, default=1, help='1-based occurrence after page/neighbor guard filtering')
    paragraph_delete_parser.add_argument('--expected-previous-contains', help='Optional previous-paragraph context guard')
    paragraph_delete_parser.add_argument('--expected-next-contains', help='Optional next-paragraph context guard')
    paragraph_delete_parser.add_argument('--max-page-after', type=int, help='Optional maximum page evidence after cleanup')
    paragraph_delete_parser.add_argument('--confirm-remove', action='store_true', required=True, help='Required explicit confirmation for destructive cleanup')
    paragraph_delete_parser.add_argument('--json', action='store_true', help='Print normalized command-bundle parser JSON')

    native_table_parser = subparsers.add_parser(
        'text-table-to-native',
        help='Bundle-backed native HWP table insertion from a Markdown/pipe table; rendered proof required',
    )
    native_table_source = native_table_parser.add_mutually_exclusive_group(required=True)
    native_table_source.add_argument('--from-file', type=Path, help='UTF-8 file containing the Markdown/pipe table source')
    native_table_source.add_argument('--text', help='Inline Markdown/pipe table source')
    native_table_parser.add_argument(
        '--confirm-native-table',
        action='store_true',
        required=True,
        help='Required explicit confirmation for native table insertion; old pipe/plain text will not be deleted',
    )
    native_table_parser.add_argument('--field-name', default='__rumi_native_table_cells__', help='Temporary field name used while filling native table cells')
    native_table_parser.add_argument('--split-by-column', help='Split into one native table per distinct value under this header, preserving the header row in each split')
    native_table_parser.add_argument('--json', action='store_true', help='Print normalized command-bundle parser JSON')


    cleanup_parser = subparsers.add_parser(
        'text-table-cleanup-selected',
        help='Bundle-backed exact active-selection source cleanup; separate from native table insertion; rendered proof required',
    )
    cleanup_source = cleanup_parser.add_mutually_exclusive_group(required=True)
    cleanup_source.add_argument('--from-file', type=Path, help='UTF-8 file containing the exact expected selected/source text')
    cleanup_source.add_argument('--text', help='Inline exact expected selected/source text')
    cleanup_parser.add_argument('--expected-hash', help='Optional expected sha256:<hex> of the exact source text')
    cleanup_parser.add_argument('--native-table-proof-ref', help='Optional rendered/native table proof label or artifact reference')
    cleanup_parser.add_argument('--native-table-proof-hash', help='Optional hash/token for the separate native table proof artifact')
    cleanup_parser.add_argument(
        '--confirm-cleanup',
        action='store_true',
        required=True,
        help='Required explicit confirmation for exact selected text deletion outside table/cell context',
    )
    cleanup_parser.add_argument('--json', action='store_true', help='Print normalized command-bundle parser JSON')

    table4_parser = subparsers.add_parser(
        'table4-anchor-range-replace',
        help='Bundle-backed guarded Table 4 range replacement with a native four-column HWP table; rendered proof required',
    )
    table4_parser.add_argument('--from-file', type=Path, required=True, help='UTF-8 Markdown/pipe table source for the replacement native table')
    table4_parser.add_argument('--section-anchor', required=True, help='Section anchor that must precede the Table 4 start anchor')
    table4_parser.add_argument('--start-anchor', required=True, help='Range start anchor included in the deleted old source text')
    table4_parser.add_argument('--end-before-anchor', required=True, help='Range end anchor; selection stops immediately before this anchor')
    table4_parser.add_argument('--required-source-basename', help='Fail unless active live source filename has this basename')
    table4_parser.add_argument('--forbid-source-basename', help='Fail if active live source filename has this basename')
    table4_parser.add_argument('--expected-range-hash', help='Optional sha256:<hex> exact selected range hash guard')
    table4_parser.add_argument('--expected-normalized-range-hash', help='Optional sha256:<hex> normalized selected range hash guard')
    table4_parser.add_argument('--caption-text', help='Optional caption text inserted immediately before the native table after deleting the selected range')
    table4_parser.add_argument('--field-name', default='__rumi_table4_native_cells__', help='Temporary field name used while filling native table cells')
    table4_parser.add_argument('--confirm-replace', action='store_true', required=True, help='Required explicit confirmation for guarded delete+native-table insertion')
    table4_parser.add_argument('--json', action='store_true', help='Print normalized command-bundle parser JSON')

    for style_command in ('style-apply', 'style-clone'):
        style_apply_parser = subparsers.add_parser(style_command, help='Guarded dump-only style clone/apply proof; no live mutation')
        style_apply_parser.add_argument('--dump-spec', action='store_true', required=True, help='Required: print guarded bundle spec only')
        style_apply_parser.add_argument('--from', dest='from_match', required=True, help='Exemplar match/anchor')
        style_apply_parser.add_argument('--to', '--to-match', dest='to_match', required=True, help='Target match/anchor')

    qa_parser = subparsers.add_parser('qa-profile', help='Bundle-backed fresh-PDF section/token/freshness QA')
    qa_parser.add_argument('--section', '--section-anchor', dest='section_anchor', help='Optional section anchor for scoped checks')
    qa_parser.add_argument('--until-anchor', help='Optional later anchor that ends the section scope')
    qa_parser.add_argument('--forbid', action='append', default=[], help='Forbidden token; repeatable')
    qa_parser.add_argument('--require', action='append', default=[], help='Required token; repeatable')
    qa_parser.add_argument('--after-anchor-forbid', action='append', default=[], help='ANCHOR::TOKEN pair; token must not appear after anchor')
    qa_parser.add_argument('--source-hash', help='Expected/forbidden source SHA256 freshness guard')
    qa_parser.add_argument('--out-dir', type=Path, required=True, help='Output directory for QA PDF and manifest')
    qa_parser.add_argument('--json', action='store_true', help='Print QA manifest JSON')
    save_parser = subparsers.add_parser('save', help='Save the active working copy and download it locally')
    save_parser.add_argument('--out', type=Path, help='Exact local destination path; no collision suffix is added')
    subparsers.add_parser('working-copy', help='Download the latest saved working copy without re-saving')
    subparsers.add_parser('undo', help='Undo the most recent live document change')
    subparsers.add_parser('redo', help='Redo the most recently undone live document change')

    table_parser = subparsers.add_parser('table', help='Create a table at the current caret position')
    table_parser.add_argument('cols', type=int)
    table_parser.add_argument('rows', type=int)

    list_parser = subparsers.add_parser('list', help='Create a list at the current caret position')
    list_parser.add_argument('count', type=int)

    screenshot_parser = subparsers.add_parser('screenshot', help='Capture the current editor state')
    screenshot_parser.add_argument(
        '--mode',
        choices=('live', 'page'),
        default='live',
        help='live captures the editor viewport; page exports PDF and renders a full document page',
    )
    screenshot_parser.add_argument('--page', type=int, default=1, help='1-based page number for --mode page')
    screenshot_parser.add_argument('--dpi', type=int, default=160, help='render DPI for --mode page')
    screenshot_parser.add_argument('--json', action='store_true', help='Print the stable JSON envelope')

    page_screenshot_parser = subparsers.add_parser(
        'page-screenshot',
        help='Export PDF and render a full document page screenshot',
    )
    page_screenshot_parser.add_argument('--page', type=int, default=1, help='1-based page number to render')
    page_screenshot_parser.add_argument('--dpi', type=int, default=160, help='render DPI')
    page_screenshot_parser.add_argument('--out', type=Path, help='Exact PNG destination path')
    page_screenshot_parser.add_argument('--out-dir', type=Path, help='Directory for the default page proof filename')
    page_screenshot_parser.add_argument('--json', action='store_true', help='Print the stable JSON envelope')

    export_parser = subparsers.add_parser('export', help='Export the active working copy to PDF')
    export_parser.add_argument('--out', type=Path, help='Exact PDF destination path')
    export_parser.add_argument('--bundle-proof', action='store_true', help='Use the bundle-backed export_pdf proof slice instead of the legacy direct export route')
    export_parser.add_argument('--json', action='store_true', help='Print the stable JSON envelope')
    close_parser = subparsers.add_parser('close', help='Close the active live document session')
    close_parser.add_argument('--json', action='store_true', help='Print the structured lifecycle response')

    type_parser = subparsers.add_parser('type', help='Replace the selection, or insert text at the caret')
    type_parser.add_argument('text')
    type_parser.add_argument('--insert-at-caret', action='store_true', help='Explicitly insert without using a cached selection')

    for command_name, help_text, position in (
        ('insert-before-anchor', 'Insert text before a text anchor using one live anchor resolution', 'before-anchor'),
        ('insert-after-anchor', 'Insert text after a text anchor using one live anchor resolution', 'after-anchor'),
        ('insert-after-paragraph', 'Insert text after the paragraph containing a text anchor', 'after-paragraph'),
        ('insert-before-heading', 'Insert text before a heading/anchor without repeated-before reversal', 'before-heading'),
    ):
        anchor_parser = subparsers.add_parser(command_name, help=help_text)
        anchor_parser.add_argument('--target', '--anchor', dest='target', required=True, help='Target text/heading anchor')
        anchor_source = anchor_parser.add_mutually_exclusive_group(required=True)
        anchor_source.add_argument('--text', help='Inline text to insert')
        anchor_source.add_argument('--from-file', type=Path, help='UTF-8 text file to insert')
        anchor_parser.set_defaults(anchor_insert_position=position)

    figure_parser = subparsers.add_parser(
        'figure-section',
        help='Insert heading, intro, optional image, caption, and body before a target heading as one logical bundle',
    )
    figure_parser.add_argument('--before-heading', '--target-heading', dest='target_heading', required=True, help='Existing next heading to insert before')
    figure_parser.add_argument('--heading', required=True, help='New section heading')
    figure_parser.add_argument('--intro', help='Intro paragraph text')
    figure_parser.add_argument('--intro-file', type=Path, help='UTF-8 intro text file')
    figure_parser.add_argument('--image', type=Path, help='Optional image file inserted between intro and caption')
    figure_parser.add_argument('--caption', help='Figure caption text')
    figure_parser.add_argument('--caption-file', type=Path, help='UTF-8 caption text file')
    figure_parser.add_argument('--body', help='Body text after caption')
    figure_parser.add_argument('--body-file', type=Path, help='UTF-8 body text file')
    figure_parser.add_argument('--width', type=float, default=None, help='Optional image width passed to Hancom/pyhwpx')
    figure_parser.add_argument('--height', type=float, default=None, help='Optional image height passed to Hancom/pyhwpx')
    figure_parser.add_argument('--sizeoption', type=int, default=None, help='Optional Hancom/pyhwpx image size option')
    figure_parser.add_argument('--treat-as-char', choices=('on', 'off'), default=None, help='Insert image as character (default: on)')
    figure_parser.add_argument('--embedded', choices=('on', 'off'), default=None, help='Embed image data in the document (default: on)')
    figure_parser.add_argument('--fit-cell', action='store_true', help='Table-cell-friendly image insertion defaults')

    image_parser = subparsers.add_parser('image', help='Insert a local PNG/JPEG/BMP at the current caret position')
    image_parser.add_argument('file', type=Path)
    image_parser.add_argument('--width', type=float, default=None, help='Optional image width passed to Hancom/pyhwpx')
    image_parser.add_argument('--height', type=float, default=None, help='Optional image height passed to Hancom/pyhwpx')
    image_parser.add_argument('--sizeoption', type=int, default=None, help='Optional Hancom/pyhwpx image size option')
    image_parser.add_argument('--treat-as-char', choices=('on', 'off'), default=None, help='Insert as character (default: on)')
    image_parser.add_argument('--embedded', choices=('on', 'off'), default=None, help='Embed image data in the document (default: on)')
    image_parser.add_argument(
        '--fit-cell',
        action='store_true',
        help='Shorthand for table-cell-friendly insertion: treat-as-char on, embedded on, sizeoption 3 unless overridden',
    )

    image_anchor_parser = subparsers.add_parser('image-at-anchor', help='Resolve a text anchor once, then insert a local PNG/JPEG/BMP there')
    image_anchor_parser.add_argument('file', type=Path)
    image_anchor_parser.add_argument('--target', '--anchor', required=True, help='Target text anchor to resolve before image insertion')
    image_anchor_parser.add_argument(
        '--position',
        choices=('before-anchor', 'after-anchor', 'after-paragraph', 'before-heading'),
        default='before-anchor',
        help='Where to place the image relative to the resolved anchor',
    )
    image_anchor_parser.add_argument('--width', type=float, default=None, help='Optional image width passed to Hancom/pyhwpx')
    image_anchor_parser.add_argument('--height', type=float, default=None, help='Optional image height passed to Hancom/pyhwpx')
    image_anchor_parser.add_argument('--sizeoption', type=int, default=None, help='Optional Hancom/pyhwpx image size option')
    image_anchor_parser.add_argument('--treat-as-char', choices=('on', 'off'), default=None, help='Insert as character (default: on)')
    image_anchor_parser.add_argument('--embedded', choices=('on', 'off'), default=None, help='Embed image data in the document (default: on)')
    image_anchor_parser.add_argument(
        '--fit-cell',
        action='store_true',
        help='Shorthand for table-cell-friendly insertion: treat-as-char on, embedded on, sizeoption 3 unless overridden',
    )

    replace_parser = subparsers.add_parser('replace', help='Disabled: atomic replace is reserved until rendered disposable-document proof exists')
    replace_parser.add_argument('target')
    replace_parser.add_argument('text')

    cell_replace_parser = subparsers.add_parser(
        'cell-replace',
        aliases=['cell-replace-exact'],
        help='Replace one table cell by anchor or by a current/anchor-table-scoped exact cell address',
    )
    cell_replace_parser.add_argument('--anchor', default=None, help='Text anchor inside the target table cell')
    cell_replace_parser.add_argument('--cell', default=None, help='Exact table cell address to target, e.g. E5')
    cell_replace_parser.add_argument('--expected-page', type=int, default=None, help='Expected live Hancom page for the target cell')
    cell_replace_parser.add_argument('--text', default=None, help='Replacement body text; use --text-file for multiline bodies')
    cell_replace_parser.add_argument('--text-file', type=Path, default=None, help='UTF-8 replacement body text file')
    cell_replace_parser.add_argument('--expect-cell', default=None, help='Expected table cell address, e.g. A1')
    cell_replace_parser.add_argument('--expect-old', default=None, help='Token that must be present before replacement and absent after')
    cell_replace_parser.add_argument('--expect-new', default=None, help='Token that must be present after replacement')
    cell_replace_parser.add_argument('--json', action='store_true', help='Print full cell-replace JSON, including raw after_text readback artifact metadata')

    fontsize_parser = subparsers.add_parser('fontsize', help='Set font size on the current selection or caret position')
    fontsize_parser.add_argument('size_pt', type=float)

    bold_parser = subparsers.add_parser('bold', help='Turn bold on or off for the proven current text selection')
    bold_parser.add_argument('value', choices=('on', 'off'))

    font_parser = subparsers.add_parser('font', help='Set font family on the current selection or caret position')
    font_parser.add_argument('face_name')

    bullet_parser = subparsers.add_parser('bullet', help='Create one bullet item at the current caret position')
    bullet_parser.add_argument('text')

    pycall_parser = subparsers.add_parser(
        'pycall',
        help='Safely call a public pyhwpx/Hwp method or read a public scalar property',
    )
    pycall_parser.add_argument('method_path')
    pycall_parser.add_argument('args_json', nargs='?', default='[]', help='JSON array of positional args')
    pycall_parser.add_argument('kwargs_json', nargs='?', default='{}', help='JSON object of keyword args')

    action_parser = subparsers.add_parser('action', help='Run a Hancom HAction by name')
    action_parser.add_argument('action_name')

    subparsers.add_parser('bundle-list', aliases=['bundles'], help='List local planner bundle recipes')

    bundle_help_parser = subparsers.add_parser('bundle-help', help='Show usage for one local planner bundle')
    bundle_help_parser.add_argument('bundle_name')

    bundle_dump_parser = subparsers.add_parser(
        'bundle-dump',
        help='Build a named local planner bundle and print strict server JSON without executing it',
    )
    bundle_dump_parser.add_argument('--with-meta', action='store_true', help='Include local where/how/changed metadata in the printed JSON')
    bundle_dump_parser.add_argument('bundle_name')
    bundle_dump_parser.add_argument('bundle_args', nargs=argparse.REMAINDER)

    tx_preview_parser = subparsers.add_parser(
        'tx-preview',
        help='Write a local safe-edit transaction preview JSON without executing it',
    )
    tx_preview_parser.add_argument('out_json', type=Path, help='Output transaction preview JSON file')
    tx_preview_parser.add_argument('tx_args', nargs=argparse.REMAINDER, help='Use: --recipe NAME [recipe args] [--with-meta]')

    tx_commit_parser = subparsers.add_parser(
        'tx-commit',
        help='Commit a tx-preview JSON by posting its exact server_payload; does not save',
    )
    tx_commit_parser.add_argument('plan_json', type=Path, help='Transaction preview JSON produced by tx-preview')
    tx_commit_parser.add_argument('--json', action='store_true', help='Print normalized local parser JSON instead of the concise human summary')

    create_bundle_parser = subparsers.add_parser(
        'create-bundle',
        aliases=['bundle-create', 'bundle-compose'],
        help='Create a local explicit command bundle JSON file without executing it',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'examples:\n'
            '  hwpx create-bundle where-proof.json --recipe where\n'
            '  hwpx create-bundle selected-proof.json --recipe selected-text-proof\n'
            '  hwpx create-bundle insert.json --recipe "insert-text-file body.txt --ack-active-target"\n'
            '  hwpx create-bundle composed.json --recipe selected-text-proof --step "where;label=where:extra-proof"'
        ),
    )
    create_bundle_parser.add_argument('out_json', type=Path, help='Output JSON file to write')
    create_bundle_parser.add_argument(
        '--recipe',
        '--from-template',
        dest='recipes',
        action='append',
        default=[],
        metavar='SPEC',
        help='Add a registry recipe spec, e.g. "where" or "insert-text-file body.txt --ack-active-target"',
    )
    create_bundle_parser.add_argument(
        '--step',
        dest='steps',
        action='append',
        default=[],
        metavar='SPEC',
        help='Add a raw safe primitive: where; get_selected_text; set_text_file;path=body.txt;ack_active_target=true',
    )
    create_bundle_parser.add_argument('--with-meta', action='store_true', help='Write local metadata wrapper with extractable server_payload')
    create_bundle_parser.add_argument('--force', action='store_true', help='Overwrite the output file if it already exists')

    bundle_run_parser = subparsers.add_parser(
        'bundle-run',
        help='Build a named local planner bundle locally and post it to /local-cli/command-bundle',
    )
    bundle_run_parser.add_argument('--json', action='store_true', help='Print normalized local parser JSON instead of the concise human summary')
    bundle_run_parser.add_argument('bundle_name')
    bundle_run_parser.add_argument('bundle_args', nargs=argparse.REMAINDER)

    bundle_parser = subparsers.add_parser(
        'bundle',
        help='Execute an explicit allowlisted pyhwpx command bundle JSON file (or - for stdin)',
    )
    bundle_parser.add_argument('--json', action='store_true', help='Print normalized local parser JSON instead of the concise human summary')
    bundle_parser.add_argument('json_file', help='JSON object with {"steps": [...]} or a raw steps array; use - for stdin')

    return parser


def _state_session_id() -> str | None:
    state = load_state()
    session_id = state.get('session_id')
    return str(session_id) if session_id else None


def _state_cas_snapshot() -> tuple[dict[str, Any], int, str | None]:
    state = load_state()
    return (
        state,
        int(state.get('state_generation', 0)),
        str(state.get('session_id') or '').strip() or None,
    )


def _candidate_root_has_reparse_component(root: Path) -> bool:
    current = Path(root)
    while True:
        try:
            if current.is_symlink():
                return True
            attributes = int(getattr(current.lstat(), 'st_file_attributes', 0))
            if attributes & 0x400:
                return True
        except OSError:
            if not current.exists():
                current = current.parent
                if current == Path(root).anchor:
                    return False
                continue
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _read_candidate_identity(root: Path | None = None) -> dict[str, Any]:
    """Read and verify the install marker against the installed source bytes.

    The default root is derived from this installed module, never from the
    caller's working directory.  The marker and source manifest are both
    untrusted metadata, so the manifest hash and every declared source file
    are checked before any identity is returned to callers.
    """

    raw_module_root = (root if root is not None else Path(__file__).parent.parent).expanduser()
    if _candidate_root_has_reparse_component(raw_module_root):
        return {}
    module_root = raw_module_root.absolute()
    marker_path = module_root / '.hwpx-install.json'
    manifest_path = module_root / 'source-manifest.json'
    try:
        if (
            not marker_path.is_file()
            or marker_path.is_symlink()
            or marker_path.stat().st_size > 64 * 1024
            or not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.stat().st_size > 8 * 1024 * 1024
        ):
            return {}
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode('utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(marker, dict) or not isinstance(manifest, dict):
        return {}
    if marker.get('schema_version') != 'hwpx/windows-install-marker/v1':
        return {}
    repository = marker.get('repository')
    commit = marker.get('commit')
    tree = marker.get('tree')
    manifest_sha256 = marker.get('source_manifest_sha256')
    if not all(isinstance(value, str) and value.strip() for value in (repository, commit, tree, manifest_sha256)):
        return {}
    repository_text = str(repository).strip()
    commit_text = str(commit).strip()
    tree_text = str(tree).strip()
    manifest_text = str(manifest_sha256).strip().lower()
    if not all((
        re.fullmatch(r'[0-9A-Fa-f]{40}', commit_text),
        re.fullmatch(r'[0-9A-Fa-f]{40}', tree_text),
        re.fullmatch(r'[0-9A-Fa-f]{64}', manifest_text),
    )):
        return {}
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_text:
        return {}
    if any(
        manifest.get(field) != expected
        for field, expected in (
            ('repository', repository_text),
            ('commit', commit_text),
            ('tree', tree_text),
        )
    ):
        return {}
    files = manifest.get('files')
    file_count = manifest.get('file_count')
    if manifest.get('schema_version') != 'hwpx/source-bundle/v1' or not isinstance(files, list):
        return {}
    if len(files) > 2048 or not isinstance(file_count, int) or file_count != len(files):
        return {}
    seen_paths: set[str] = set()
    module_root_resolved = module_root.resolve()
    for entry in files:
        if not isinstance(entry, dict):
            return {}
        relative = entry.get('path')
        declared_size = entry.get('size')
        declared_hash = entry.get('sha256')
        if (
            not isinstance(relative, str)
            or not relative.strip()
            or '\\' in relative
            or not isinstance(declared_size, int)
            or declared_size < 0
            or not isinstance(declared_hash, str)
            or not re.fullmatch(r'[0-9A-Fa-f]{64}', declared_hash)
        ):
            return {}
        relative_path = Path(relative)
        if relative_path.is_absolute() or '..' in relative_path.parts:
            return {}
        normalized = relative_path.as_posix()
        if normalized in seen_paths:
            return {}
        seen_paths.add(normalized)
        installed_path = module_root / relative_path
        try:
            installed_resolved = installed_path.resolve()
            installed_resolved.relative_to(module_root_resolved)
            current = module_root
            for part in relative_path.parts:
                current = current / part
                if current.is_symlink():
                    return {}
            if not installed_resolved.is_file() or installed_resolved.stat().st_size != declared_size:
                return {}
            if _sha256_file(installed_resolved) != declared_hash.lower():
                return {}
        except (OSError, ValueError):
            return {}
    identity_source = manifest.get('identity_source')
    identity_verified = manifest.get('identity_verified')
    if (
        identity_source not in {'git', 'asserted-gitless'}
        or not isinstance(identity_verified, bool)
        or (identity_source == 'git' and not identity_verified)
        or (identity_source == 'asserted-gitless' and identity_verified)
    ):
        return {}
    marker_generation = marker.get('candidate_generation')
    expected_generation = f'{commit_text}:{tree_text}:{manifest_text}'
    if marker_generation is not None and marker_generation != expected_generation:
        return {}
    return {
        'repository': repository_text,
        'commit': commit_text,
        'tree': tree_text,
        'manifest_sha256': manifest_text,
        'source_manifest_sha256': manifest_text,
        'candidate_generation': expected_generation,
        'identity_source': identity_source,
        'identity_verified': identity_verified,
        'candidate_identity_authenticated': identity_source == 'git' and identity_verified is True,
    }


def _read_required_json_mapping(path: Path, *, label: str, max_bytes: int = 64 * 1024) -> dict[str, Any]:
    source = path.expanduser().resolve()
    try:
        if not source.is_file():
            raise ProofPacketError(f'{label} JSON file was not found: {source}')
        if source.stat().st_size > max_bytes:
            raise ProofPacketError(f'{label} JSON file exceeds the bounded size limit: {source}')
        payload = json.loads(source.read_bytes().decode('utf-8-sig'))
    except ProofPacketError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProofPacketError(f'{label} JSON file could not be read safely: {source}') from exc
    if not isinstance(payload, dict):
        raise ProofPacketError(f'{label} JSON file must contain an object: {source}')
    return payload


def _record_native_border_readback(
    *,
    pre_quit_path: Path,
    persisted_path: Path,
    target_identity_path: Path,
    destination: Path,
) -> dict[str, Any]:
    identity = _read_candidate_identity()
    state_before_border, border_expected_generation, border_expected_session = _state_cas_snapshot()
    if not identity:
        raise ProofPacketError('native border readback requires a complete installed candidate identity marker bound to the installed module root')
    pre_quit_source = pre_quit_path.expanduser().resolve()
    persisted_source = persisted_path.expanduser().resolve()
    target_source = target_identity_path.expanduser().resolve()
    output = destination.expanduser().resolve()
    if output in {pre_quit_source, persisted_source, target_source}:
        raise ProofPacketError('native border readback output must not overwrite an input JSON file')
    pre_quit = _read_required_json_mapping(pre_quit_source, label='pre-quit readback')
    persisted = _read_required_json_mapping(persisted_source, label='persisted readback')
    target_identity = _read_required_json_mapping(target_source, label='target identity')
    sealed = seal_native_border_readback(
        destination=output,
        candidate_generation=identity['candidate_generation'],
        source_manifest_sha256=identity['manifest_sha256'],
        target_identity=target_identity,
        pre_quit_readback=pre_quit,
        persisted_readback=persisted,
    )
    updated_state = update_state(
        lambda state: {
            **state,
            'repository': identity['repository'],
            'commit': identity['commit'],
            'tree': identity['tree'],
            'manifest_sha256': identity['manifest_sha256'],
            'source_manifest_sha256': identity['manifest_sha256'],
            'candidate_generation': identity['candidate_generation'],
            'target_identity': target_identity,
            'native_border_target_identity': target_identity,
            'last_native_border_readback_path': str(output),
        },
        expected_generation=border_expected_generation,
        expected_session_id=border_expected_session,
    )
    return {
        'schema_version': 'local-cli/native-border-readback-command/v1',
        'ok': True,
        'command': 'native-border-readback',
        'artifact_path': str(output),
        'proof_binding': {
            'candidate_generation': identity['candidate_generation'],
            'source_manifest_sha256': identity['manifest_sha256'],
            'target_identity': target_identity,
            'session_id': updated_state.get('session_id'),
        },
        'artifact': sealed,
    }


def _with_lifecycle_identity(payload: dict[str, Any], *, base_url: str, command: str) -> dict[str, Any]:
    """Add cached session identity to command output used by native acceptance."""

    state = load_state()
    result = dict(payload)
    session_id = state.get('session_id')
    result.update({
        'base_url': base_url,
        'command': command,
        'session_id': session_id,
        'working_copy_id': result.get('working_copy_id') or session_id,
        'source_path': state.get('source_path'),
        'managed_fixture': state.get('source_path'),
        'live_session_bound': True,
    })
    result.update(_read_candidate_identity())
    return result


def _with_status_identity(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.update({
        'schema_version': 'local-cli/status/v1',
        'command': 'status',
    })
    result.update(_read_candidate_identity())
    return result


def _resolve_base_url(explicit_base_url: str | None) -> str:
    if explicit_base_url and explicit_base_url.strip():
        return explicit_base_url.rstrip('/')
    cached_base_url = str(load_state().get('base_url') or '').strip()
    if cached_base_url:
        return cached_base_url.rstrip('/')
    return DEFAULT_BASE_URL


def _last_proof_artifact(state: dict[str, Any]) -> tuple[str, str] | None:
    proof_pages = state.get('last_export_proof_page_paths')
    if isinstance(proof_pages, list):
        for value in proof_pages:
            if isinstance(value, str) and value.strip():
                return 'export-proof rendered page', value
    candidates = (
        ('rendered page proof', state.get('last_page_screenshot_path')),
        ('live editor proof', state.get('last_screenshot_path')),
        ('exported PDF proof source', state.get('last_export_path')),
        ('finish proof bundle', state.get('last_finish_lane_bundle_path')),
    )
    for role, value in candidates:
        if isinstance(value, str) and value.strip():
            return role, value
    return None


def _print_workflow_help() -> None:
    print('HWPX workflow: status -> open -> find/where/select -> edit -> screenshot/export-proof -> save -> close')
    print('')
    print('Target pipeline:')
    print('1. local input planner -> explicit command bundle')
    print('2. thin server executor -> pyhwpx/Hancom primitive under runtime lock')
    print('3. local output parser -> where/how/changed proof for the operator')
    print('')
    print('Proof rules:')
    print('- Use rendered artifacts: export-proof-range, page-screenshot, PDF render review, or live editor screenshot as appropriate.')
    print('- Page count is incidental metadata only; never use it as pass/fail proof.')
    print('- Layout-risk edits need no-clipping/no-overflow review and preserved tables/images/boxes/bullets/spacing/page breaks.')
    print(f'- Disabled/dump-only specs are target proof only: {NO_MUTATION_TEXT}.')
    print('')
    print('Next action: run `hwpx status`, then `hwpx open <file>` or `hwpx where` if a session is already open.')


def _print_command_status() -> None:
    command_status = build_command_status()
    print('command status:')
    print('derived from: argparse parser + local bundle registry')
    print('runtime: unknown (local metadata only; run `hwpx status` or `hwpx session-health` for a non-mutating route probe)')
    print('legend: bundle-backed = local/bundle path; direct-backlog = direct-route migration backlog; disabled = no live mutation')
    for command in sorted(command_status):
        meta = command_status[command]
        print(f"- {command}: {meta['status']} - {meta['note']}")
    print(f'bundle registry recipes: {", ".join(sorted(recipe.name for recipe in iter_bundle_recipes()))}')
    print(f'blocked bundle recipes: {", ".join(sorted(blocked_bundle_names())) or "none"}')
    print(f'bundle server ops: {", ".join(sorted(BUNDLE_SERVER_OPS))}')
    print('next: use bundle-backed commands when possible; treat direct-backlog commands as migration targets, not new architecture.')


def _read_manifest_data(manifest_path: Path | None) -> dict[str, Any] | None:
    if manifest_path is None:
        return None
    try:
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def _artifact_envelope(
    *,
    role: str,
    path: Path,
    next_step: str,
    manifest_path: Path | None = None,
    command: str | None = None,
) -> dict[str, Any]:
    proof = str(path)
    if manifest_path is not None:
        proof = f'{path} (manifest: {manifest_path})'
    envelope = build_envelope(
        where=str(path),
        how=role,
        changed='artifact written/downloaded; original source file untouched by local CLI',
        proof=proof,
        next_step=next_step,
        artifact_role=role,
        artifact_path=path,
        manifest_path=manifest_path,
        manifest_data=_read_manifest_data(manifest_path),
    )
    envelope.update(_read_candidate_identity())
    if command:
        envelope['command'] = command
    return envelope


def _print_artifact_result(*, role: str, path: Path, next_step: str, manifest_path: Path | None = None, json_output: bool = False, command: str | None = None) -> None:
    envelope = _artifact_envelope(role=role, path=path, next_step=next_step, manifest_path=manifest_path, command=command)
    print(dumps_envelope_json(envelope) if json_output else format_human_envelope(envelope))


def _print_lifecycle_result(*, result: str = 'ok', where: str, how: str, changed: str, proof: str, next_step: str) -> None:
    envelope = build_envelope(result=result, where=where, how=how, changed=changed, proof=proof, next_step=next_step)
    print(format_human_envelope(envelope))


def _print_status(payload: dict[str, Any]) -> None:
    state = load_state()
    active_document = payload.get('active_document') or state.get('source_filename') or 'none'
    session = payload.get('session_id') or state.get('session_id') or 'none'
    working_copy = payload.get('working_copy_path') or state.get('last_saved_working_copy_path') or 'not downloaded yet'
    last_proof = _last_proof_artifact(state)
    proof_text = f'{last_proof[0]}: {last_proof[1]}' if last_proof else 'none'
    next_action = payload.get('next_action') or 'open a file or run `hwpx help workflow`'

    print('result: ok')
    print(f'where: active document={active_document}; session={session}')
    print('how: direct /local-cli/status plus non-mutating command-bundle route probe')
    print('changed: none')
    print(f'proof: {proof_text}')
    print(f'next: {next_action}')
    print(f"runtime: {'up' if payload.get('runtime_up') else 'down'}")
    print(f"Hancom attached: {'yes' if payload.get('hancom_attached') else 'no'}")
    print(f"API ready: {'yes' if payload.get('api_ready') else 'no'}")
    print(f"active document: {active_document}")
    print(f"session: {session}")
    print(f"live session: {'yes' if payload.get('live_session_bound') else 'no'}")
    dirty = payload.get('working_copy_dirty')
    print(f"dirty: {'unknown' if dirty is None else ('yes' if dirty else 'no')}")
    print(f"working copy: {working_copy}")
    if state.get('source_path'):
        print(f"source: {state.get('source_path')}")
    print(f"last proof artifact: {proof_text}")
    last_proof_bundle = state.get('last_export_manifest_path') or state.get('last_page_screenshot_manifest_path') or state.get('last_finish_lane_bundle_path')
    print(f"last proof bundle/manifest: {last_proof_bundle or 'none'}")
    print(f"blocked: {payload.get('blocked_reason') or 'none'}")
    print(f"next action: {next_action}")
    if payload.get('command_bundle_route_active') is not None:
        print(f"command-bundle route: {'active' if payload.get('command_bundle_route_active') else 'unavailable'}")
    if payload.get('probe_status'):
        print(f"command-bundle probe: {payload.get('probe_status')}")
    if payload.get('route_error'):
        print(f"route error: {payload.get('route_error')}")
    if payload.get('server_primitive_version'):
        print(f"server primitives: {payload.get('server_primitive_version')}")
    if payload.get('command_package_op_count') is not None:
        print(f"command packages: {payload.get('command_package_op_count')} ops")
    if payload.get('command_package_ops'):
        print(f"command package sample: {', '.join(str(item) for item in payload.get('command_package_ops')[:8])}")
    if payload.get('command_package_revision'):
        print(f"command package revision: {payload.get('command_package_revision')}")

def _probe_command_bundle_route(base_url: str) -> dict[str, Any]:
    """Return route health without needing an active document session."""

    try:
        post_json(base_url, '/local-cli/command-bundle', {'steps': [], 'session_id': _state_session_id()})
    except ApiError as exc:
        # A 400 validation error means the route is mounted and reached; 404/405
        # means the running Windows API likely has not reloaded the source yet.
        if exc.status_code == 400:
            return {
                'command_bundle_route_active': True,
                'probe_status': 'active-validation-error',
                'route_error': exc.message,
                'server_primitive_version': 'local-cli-command-bundle/v1',
            }
        return {
            'command_bundle_route_active': False,
            'probe_status': f'error-{exc.status_code or "unreachable"}',
            'route_error': exc.message,
            'error': exc.message,
        }
    return {
        'command_bundle_route_active': True,
        'probe_status': 'active-unexpected-empty-bundle-accepted',
        'server_primitive_version': 'local-cli-command-bundle/v1',
    }


def _print_session_health(base_url: str) -> None:
    state = load_state()
    print(f"base url: {base_url}")
    print(f"cached session: {state.get('session_id') or 'none'}")
    if state.get('source_path'):
        source_path = Path(str(state.get('source_path'))).expanduser()
        print(f"source path: {source_path}")
        print(f"source exists: {'yes' if source_path.exists() else 'no'}")
    try:
        status = get_json(base_url, '/local-cli/status')
        print(f"runtime: {'up' if status.get('runtime_up') else 'down'}")
        print(f"live session: {'yes' if status.get('live_session_bound') else 'no'}")
        if status.get('blocked_reason'):
            print(f"blocked: {status.get('blocked_reason')}")
    except ApiError as exc:
        print(f'runtime: unknown ({exc.message})')
    route = _probe_command_bundle_route(base_url)
    print(f"command-bundle route: {'active' if route.get('command_bundle_route_active') else 'unavailable'}")
    if route.get('probe_status'):
        print(f"command-bundle probe: {route.get('probe_status')}")
    if route.get('route_error') or route.get('error'):
        print(f"route error: {route.get('route_error') or route.get('error')}")
    if route.get('server_primitive_version'):
        print(f"server primitives: {route.get('server_primitive_version')}")


def _print_state() -> None:
    state_path = default_state_path()
    state = load_state(state_path)
    print(f'state file: {state_path}')
    if not state:
        print('cached state: none')
        print(f'default base url: {DEFAULT_BASE_URL}')
        return

    print(f"base url: {state.get('base_url') or DEFAULT_BASE_URL}")
    print(f"session: {state.get('session_id') or 'none'}")
    print(f"source: {state.get('source_path') or state.get('source_filename') or 'none'}")
    print(f"last saved working copy: {state.get('last_saved_working_copy_path') or 'none'}")
    print(f"last screenshot: {state.get('last_screenshot_path') or 'none'}")
    print(f"last page screenshot: {state.get('last_page_screenshot_path') or 'none'}")
    print(f"last export: {state.get('last_export_path') or 'none'}")
    print(f"last proof bundle: {state.get('last_finish_lane_bundle_path') or 'none'}")


def _print_matches(payload: dict[str, Any]) -> None:
    matches = payload.get('matches') if isinstance(payload.get('matches'), list) else []
    if not matches:
        print('no matches')
        return
    for match in matches:
        number = match.get('number')
        section = Path(str(match.get('section') or '')).name
        paragraph = match.get('section_paragraph_index')
        excerpt = str(match.get('excerpt') or '').strip()
        line = f'{number}. [{section}:{paragraph}]'
        page_candidate = match.get('page_candidate')
        if page_candidate not in (None, ''):
            line += f' page~{page_candidate}'
        table = match.get('table') if isinstance(match.get('table'), dict) else {}
        if match.get('inside_table') is True:
            line += f" table {table.get('cell_addr') or '?'}"
        elif match.get('inside_table') is False or table:
            line += ' outside-table'
        digest = str(match.get('normalized_hash') or '')
        if digest.startswith('sha256:'):
            line += f' {digest[:19]}'
        if excerpt:
            line += f' {excerpt}'
        print(line)
        headings = match.get('nearby_headings') if isinstance(match.get('nearby_headings'), list) else []
        if headings:
            print(f"   heading: {' > '.join(str(item) for item in headings[-3:])}")
        context = match.get('context') if isinstance(match.get('context'), dict) else {}
        before = context.get('before') if isinstance(context.get('before'), list) else []
        after = context.get('after') if isinstance(context.get('after'), list) else []
        if before:
            print(f"   ctx before: {' / '.join(str(item) for item in before[-2:])}")
        if after:
            print(f"   ctx after: {' / '.join(str(item) for item in after[:2])}")
        warnings = match.get('warnings') if isinstance(match.get('warnings'), list) else []
        for warning in warnings[:2]:
            print(f'   warning: {warning}')


def _print_info(payload: dict[str, Any]) -> None:
    context = str(payload.get('context') or '').strip()
    if context:
        print(context)
    else:
        print('no context')


def _print_current_state(payload: dict[str, Any]) -> None:
    context = payload.get('context') if isinstance(payload.get('context'), dict) else {}
    position = payload.get('cursor_summary')
    if not position:
        caret_pos = payload.get('caret_pos')
        if caret_pos not in (None, ''):
            position = f'pos {caret_pos}'
    if not position and payload.get('cell_addr'):
        position = f"cell {payload.get('cell_addr')}"
    if position:
        print(f'position: {position}')

    selection = payload.get('selection_summary')
    if selection:
        print(f'selection: {selection}')

    current = payload.get('current_paragraph_preview') or context.get('current_paragraph_preview')
    if current:
        print(f'current: {current}')

    warning = payload.get('warning')
    if warning:
        print(f'warning: {warning}')


def _format_proof_value(value: Any) -> str:
    if isinstance(value, dict):
        parts = []
        for subkey, subvalue in value.items():
            if subvalue in (None, ''):
                continue
            if isinstance(subvalue, (dict, list, tuple)):
                rendered = json.dumps(subvalue, ensure_ascii=False, separators=(',', ':'), default=str)
            else:
                rendered = str(subvalue)
            if len(rendered) > 96:
                rendered = rendered[:95].rstrip() + '…'
            parts.append(f'{subkey}={rendered}')
            if len(parts) >= 5:
                break
        return '{' + ', '.join(parts) + '}'
    if isinstance(value, (list, tuple)):
        rendered = json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
    else:
        rendered = str(value)
    if len(rendered) > 120:
        rendered = rendered[:119].rstrip() + '…'
    return rendered


def _print_command_payload(payload: dict[str, Any]) -> None:
    print(payload.get('summary') or 'ok')
    _print_current_state(payload)
    for key in ('where', 'how', 'changed', 'next'):
        value = payload.get(key)
        if value not in (None, ''):
            print(f'{key}: {value}')
    proof = payload.get('proof')
    if isinstance(proof, dict):
        proof_parts = []
        for key in (
            'operation',
            'scope',
            'proof_source',
            'selection_required',
            'selected_text_preview',
            'selected_text_len',
            'selected_text_source',
            'replaced_text_preview',
            'replaced_text_len',
            'replaced_text_hash',
            'inserted_text_len',
            'inserted_text_hash',
            'before_has_selection',
            'restored_has_selection',
            'after_has_selection',
            'before_selected_pos',
            'after_selected_pos',
            'caret_pos_before',
            'caret_pos_after',
            'style',
            'selection_cache_cleared',
            'document_is_modified_before',
            'document_is_modified_after',
            'method',
            'after_paragraph_preview',
            'after_paragraph_hash',
        ):
            if key in proof and proof.get(key) not in (None, ''):
                proof_parts.append(f'{key}={_format_proof_value(proof.get(key))}')
        if proof_parts:
            print('proof: ' + '; '.join(proof_parts))


def _print_where(payload: dict[str, Any]) -> None:
    print(f"document: {payload.get('active_document') or 'unknown'}")
    print(f"working copy: {payload.get('working_copy_id') or 'unknown'}")
    caret = payload.get('cursor') if isinstance(payload.get('cursor'), dict) else None
    if caret:
        print(f"caret: {payload.get('cursor_summary') or caret.get('pos') or 'unknown'}")
    else:
        print('caret: unknown')
    print(f"selection: {payload.get('selection_summary') or 'none'}")
    if payload.get('document_path'):
        print(f"path: {payload.get('document_path')}")
    if payload.get('document_is_modified') is not None:
        print(f"modified: {'yes' if payload.get('document_is_modified') else 'no'}")
    if payload.get('caret_in_table_cell') is not None:
        print(f"in cell: {'yes' if payload.get('caret_in_table_cell') else 'no'}")
    if payload.get('page_count') not in (None, ''):
        print(f"pages: {payload.get('page_count')}")
    if payload.get('current_paragraph_preview'):
        print(f"current: {payload.get('current_paragraph_preview')}")


def _read_optional_text_arg(*, inline: str | None, file_path: Path | None, label: str) -> str | None:
    if inline is not None and file_path is not None:
        raise ApiError(f'Use either --{label} or --{label}-file, not both.')
    if file_path is None:
        return inline
    path = file_path.expanduser()
    if not path.exists() or not path.is_file():
        raise ApiError(f'{label} file not found: {path}')
    text = path.read_text(encoding='utf-8')
    if not text:
        raise ApiError(f'{label} file is empty: {path}')
    return text


def _parse_json_cli_value(raw: str, *, expected_type: type, label: str) -> Any:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f'{label} must be valid JSON: {exc.msg}') from exc
    if not isinstance(value, expected_type):
        expected_label = 'array' if expected_type is list else 'object'
        raise ApiError(f'{label} must be a JSON {expected_label}.')
    return value


def _print_macro_payload(payload: dict[str, Any]) -> None:
    print(payload.get('summary') or 'ok')
    if payload.get('result_type'):
        result_preview = json.dumps(payload.get('result_preview'), ensure_ascii=False, default=str)
        print(f"result: {payload.get('result_type')} {result_preview}")
    if payload.get('cursor_summary'):
        print(f"position: {payload.get('cursor_summary')}")
    if payload.get('selection_summary'):
        print(f"selection: {payload.get('selection_summary')}")
    if payload.get('warning'):
        print(f"warning: {payload.get('warning')}")



def _write_created_bundle(out_path: Path, *, recipes: list[str], steps: list[str], with_meta: bool, force: bool) -> None:
    spec = build_created_bundle(recipes, steps)
    destination = out_path.expanduser()
    if destination.exists() and not force:
        raise ApiError(f'Output file already exists: {destination}. Use --force to overwrite.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = spec.debug_payload() if with_meta else spec.server_payload()
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    server_steps = spec.server_payload()['steps']
    ops = [str(step.get('op') or '?') for step in server_steps]
    recipe_sources = [str(source.get('name')) for source in spec.sources if source.get('type') == 'recipe']
    print(f'created: {destination}')
    print(f'steps: {len(server_steps)}')
    if recipe_sources:
        print(f'recipes: {", ".join(recipe_sources)}')
    print(f'ops: {", ".join(ops)}')
    print(f'next: hwpx bundle {destination}')
    if with_meta:
        print('note: metadata is local only; hwpx bundle extracts server_payload before sending strict steps JSON.')

def _paragraph_style_apply_bundle_args(args: argparse.Namespace) -> list[str]:
    bundle_args = ['--match', args.match, '--expected-page', str(args.expected_page)]
    if args.keep_with_next is not None:
        bundle_args.extend(['--keep-with-next', args.keep_with_next])
    if args.widow_orphan is not None:
        bundle_args.extend(['--widow-orphan', args.widow_orphan])
    if args.pagebreak_before is not None:
        bundle_args.extend(['--pagebreak-before', str(args.pagebreak_before)])
    if args.confirm_layout:
        bundle_args.append('--confirm-layout')
    return bundle_args


def _paragraph_delete_bundle_args(args: argparse.Namespace) -> list[str]:
    bundle_args = ['--match', args.match, '--expected-page', str(args.expected_page)]
    if args.occurrence_on_page is not None:
        bundle_args.extend(['--occurrence-on-page', str(args.occurrence_on_page)])
    if args.expected_previous_contains:
        bundle_args.extend(['--expected-previous-contains', args.expected_previous_contains])
    if args.expected_next_contains:
        bundle_args.extend(['--expected-next-contains', args.expected_next_contains])
    if args.max_page_after is not None:
        bundle_args.extend(['--max-page-after', str(args.max_page_after)])
    if args.confirm_remove:
        bundle_args.append('--confirm-remove')
    return bundle_args


def _parse_tx_preview_args(raw_args: list[str]) -> tuple[str, list[str], bool, bool]:
    tokens = list(raw_args or [])
    with_meta = False
    force = False
    cleaned: list[str] = []
    for token in tokens:
        if token == '--with-meta':
            with_meta = True
        elif token == '--force':
            force = True
        else:
            cleaned.append(token)
    if cleaned.count('--recipe') != 1:
        raise ApiError('tx-preview requires exactly one --recipe NAME followed by recipe args.')
    recipe_index = cleaned.index('--recipe')
    if recipe_index + 1 >= len(cleaned):
        raise ApiError('tx-preview --recipe requires a recipe name.')
    if recipe_index != 0:
        raise ApiError('tx-preview syntax is: hwpx tx-preview <out.json> --recipe NAME [recipe args] [--with-meta]')
    recipe_name = cleaned[recipe_index + 1]
    recipe_args = cleaned[recipe_index + 2:]
    return recipe_name, recipe_args, with_meta, force


def _write_tx_preview(out_path: Path, *, raw_args: list[str]) -> None:
    recipe_name, recipe_args, with_meta, force = _parse_tx_preview_args(raw_args)
    spec = build_named_bundle(recipe_name, recipe_args)
    destination = out_path.expanduser()
    if destination.exists() and not force:
        raise ApiError(f'Transaction preview file already exists: {destination}. Use --force to overwrite.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    session_id = _state_session_id()
    if not session_id:
        raise ApiError('tx-preview requires an active cached session_id. Open a document first so tx-commit cannot retarget whichever document is active later.')
    server_payload = spec.server_payload()
    server_payload['session_id'] = session_id
    plan: dict[str, Any] = {
        'schema_version': 'local-cli/tx-preview/v1',
        'created_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'recipe': recipe_name,
        'recipe_args': recipe_args,
        'metadata': spec.local_metadata(),
        'server_payload': server_payload,
        'commit_route': '/local-cli/command-bundle',
        'auto_save': False,
        'proof_required_before_save': True,
        'next': f'hwpx tx-commit {destination}; then run rendered proof before any save.',
    }
    if with_meta:
        plan['bundle_debug'] = spec.debug_payload()
    destination.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    ops = [str(step.get('op') or '?') for step in server_payload.get('steps', []) if isinstance(step, dict)]
    print(f'transaction preview written: {destination}')
    print('preview only: no server call, no document mutation, no save')
    print(f'recipe: {recipe_name}')
    print(f'ops: {", ".join(ops)}')
    print(f'commit: hwpx tx-commit {destination}')
    print('after commit: run rendered proof before save; tx-commit does not auto-save.')


def _load_tx_server_payload(plan_json: Path) -> dict[str, Any]:
    path = plan_json.expanduser()
    if not path.exists() or not path.is_file():
        raise ApiError(f'Transaction preview JSON not found: {plan_json}')
    try:
        plan = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ApiError(f'Transaction preview JSON must be valid JSON: {exc.msg}') from exc
    if not isinstance(plan, dict):
        raise ApiError('Transaction preview JSON must be an object.')
    server_payload = plan.get('server_payload')
    if not isinstance(server_payload, dict):
        raise ApiError('Transaction preview JSON must include object server_payload.')
    steps = server_payload.get('steps')
    if not isinstance(steps, list) or not steps:
        raise ApiError('Transaction server_payload must include a non-empty steps array.')
    session_id = server_payload.get('session_id')
    if not isinstance(session_id, str) or not session_id.strip():
        raise ApiError('Transaction server_payload must include a non-empty session_id from tx-preview; refusing to fall back to the active document.')
    return server_payload


def _load_bundle_payload(source: str) -> dict[str, Any]:
    if source == '-':
        raw = sys.stdin.read()
    else:
        path = Path(source).expanduser()
        if not path.exists() or not path.is_file():
            raise ApiError(f'Bundle JSON file not found: {source}')
        raw = path.read_text(encoding='utf-8')
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f'Bundle JSON must be valid JSON: {exc.msg}') from exc
    if isinstance(payload, list):
        payload = {'steps': payload}
    if not isinstance(payload, dict):
        raise ApiError('Bundle JSON must be an object or a raw steps array.')
    if 'server_payload' in payload:
        server_payload = payload.get('server_payload')
        if not isinstance(server_payload, dict):
            raise ApiError('Bundle metadata wrapper server_payload must be an object.')
        payload = server_payload
    steps = payload.get('steps')
    if not isinstance(steps, list):
        raise ApiError('Bundle JSON must include a steps array.')
    return {'steps': steps, 'session_id': _state_session_id()}


def _print_bundle_list() -> None:
    print('local planner bundles:')
    for recipe in iter_bundle_recipes():
        print(f'- {recipe.name}: {recipe.summary}')
    blocked = blocked_bundle_names()
    if blocked:
        print('blocked first-pass recipes:')
        for name, reason in blocked.items():
            print(f'- {name}: {reason}')
    print('metadata: where/how/changed stays local; bundle-run sends only strict steps JSON to the server.')


def _print_bundle_plan(name: str, spec_payload: dict[str, str]) -> None:
    print(f"bundle: {name}")
    print(f"where: {spec_payload.get('where')}")
    print(f"how: {spec_payload.get('how')}")
    print(f"changed: {spec_payload.get('changed')}")


def _execute_named_bundle(
    base_url: str,
    bundle_name: str,
    bundle_args: list[str] | None = None,
    *,
    session_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Build a local bundle, send strict steps JSON, and return raw server output."""

    spec = build_named_bundle(bundle_name, bundle_args or [])
    request_payload = spec.server_payload()
    request_payload['session_id'] = _state_session_id() if session_id is None else session_id
    return spec, post_json(base_url, '/local-cli/command-bundle', request_payload)


def _reopen_fresh_session(base_url: str, source_hwp: Path | None) -> dict[str, Any]:
    state = load_state()
    state_generation = int(state.get('state_generation', 0))
    source = source_hwp or (Path(str(state.get('source_path'))).expanduser() if state.get('source_path') else None)
    if source is None:
        raise ApiError('--fresh-session requires --source-hwp or a cached source path from `hwpx open`.')
    source = source.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise ApiError(f'Fresh-session source file not found: {source}')
    suffix = source.suffix.lower()
    if suffix not in {'.hwp', '.hwpx'}:
        raise ApiError(f'Fresh-session source must be .hwp or .hwpx, got: {suffix or "<none>"}')
    existing_session_id = str(state.get('session_id') or '').strip() or None
    if existing_session_id:
        status = get_json(base_url, '/local-cli/status')
        if status.get('live_session_bound'):
            if status.get('working_copy_dirty'):
                raise ApiError('Refusing --fresh-session while the active live session is dirty. Save/close it first.')
            post_json(base_url, '/local-cli/close', {'session_id': existing_session_id})
            clear_session_binding(
                expected_generation=state_generation,
                expected_session_id=existing_session_id,
            )
            state = load_state()
            state_generation = int(state.get('state_generation', 0))
        else:
            clear_session_binding(
                expected_generation=state_generation,
                expected_session_id=existing_session_id,
            )
            state = load_state()
            state_generation = int(state.get('state_generation', 0))
    else:
        # Clear stale non-session fields through the same locked transaction so
        # the subsequent open cannot overwrite a concurrently changed state.
        clear_session_binding(expected_generation=state_generation)
        state = load_state()
        state_generation = int(state.get('state_generation', 0))
    reopen_expected_session = str(state.get('session_id') or '').strip() or None
    payload = post_file(base_url, '/local-cli/open', field_name='file', file_path=source)
    update_state(
        lambda _state: {
            'base_url': base_url,
            'session_id': payload.get('session_id'),
            'source_filename': payload.get('source_filename') or source.name,
            'source_path': str(source),
            'fresh_session_reopened_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
            **_read_candidate_identity(),
        },
        expected_generation=state_generation,
        expected_session_id=reopen_expected_session,
    )
    return {
        'requested': True,
        'source_hwp_path': str(source),
        'session_id': payload.get('session_id'),
        'source_filename': payload.get('source_filename') or source.name,
    }


def _print_bundle_payload(payload: dict[str, Any], *, json_output: bool = False) -> None:
    normalized = normalize_command_bundle(payload)
    if json_output:
        print(dumps_normalized_json(normalized))
    else:
        print(format_command_bundle_human(normalized))


def _print_image_payload(payload: dict[str, Any]) -> None:
    print(payload.get('summary') or 'ok')
    if payload.get('cursor_summary'):
        print(f"position: {payload.get('cursor_summary')}")
    if payload.get('selection_summary'):
        print(f"selection: {payload.get('selection_summary')}")


def _print_cell_replace_payload(payload: dict[str, Any]) -> None:
    print(payload.get('summary') or 'ok')
    if payload.get('cell_addr'):
        print(f"cell: {payload.get('cell_addr')}")
    if payload.get('before_preview'):
        print(f"before: {payload.get('before_preview')}")
    if payload.get('after_preview'):
        print(f"after: {payload.get('after_preview')}")
    if payload.get('before_hash') or payload.get('after_hash'):
        print(f"hash: {payload.get('before_hash') or '?'} -> {payload.get('after_hash') or '?'}")
    readback = payload.get('raw_target_readback') if isinstance(payload.get('raw_target_readback'), dict) else {}
    if readback:
        print(f"raw after_text: {readback.get('raw_text_path')}")
        print(f"raw sha256: {readback.get('raw_sha256')}")
        print(f"raw lines: {readback.get('line_count')}")
        if readback.get('manifest_path'):
            print(f"raw manifest: {readback.get('manifest_path')}")
        if readback.get('failures'):
            print(f"raw failures: {', '.join(str(item) for item in readback.get('failures') or [])}")
    strategy = payload.get('insert_strategy') if isinstance(payload.get('insert_strategy'), dict) else {}
    if strategy:
        print(f"strategy: {strategy.get('method') or strategy.get('strategy')}/{strategy.get('attempt_mode') or 'default'}")
    warnings = payload.get('warnings') if isinstance(payload.get('warnings'), list) else []
    for warning in warnings:
        print(f'warning: {warning}')
    _print_current_state(payload)


def _find_proof_out_dir(state: dict[str, Any], *, match_number: int, page: int) -> Path:
    source_path = Path(str(state.get('source_path') or state.get('source_filename') or 'document.hwpx')).expanduser()
    base = source_path.parent / f'{source_path.stem}-find-match-{match_number:03d}-page-{page:03d}'
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = base.with_name(f'{base.name}-{index}')
        if not candidate.exists():
            return candidate
    return base


def _artifact_destination(
    kind: str,
    *,
    page: int | None = None,
    out: Path | None = None,
    out_dir: Path | None = None,
    state: dict[str, Any] | None = None,
) -> Path:
    """Return a portable artifact destination with explicit-output precedence.

    ``--out`` is an exact caller contract and is never auto-suffixed.  When
    ``--out-dir`` is supplied only the default filename is derived from the
    cached source state and collision suffixing remains enabled.
    """

    if out is not None:
        return out.expanduser()
    cached_state = state if state is not None else load_state()
    source_path_raw = cached_state.get('source_path')
    source_filename = str(cached_state.get('source_filename') or 'document.hwpx')
    source_filename_path = Path(source_filename)
    if source_path_raw:
        source_path = Path(str(source_path_raw)).expanduser()
        parent = source_path.parent
        stem = source_path.stem
        source_suffix = source_path.suffix or source_filename_path.suffix or '.hwpx'
    else:
        parent = Path.cwd()
        stem = source_filename_path.stem
        source_suffix = source_filename_path.suffix or '.hwpx'
    if out_dir is not None:
        parent = out_dir.expanduser()

    if kind == 'screenshot':
        candidate = parent / f'{stem}-screenshot.png'
    elif kind in {'page-screenshot', 'page_screenshot'}:
        page_number = page or 1
        candidate = parent / f'{stem}-page-{page_number:03d}.png'
    elif kind == 'export':
        candidate = parent / f'{stem}.pdf'
    elif kind in {'working-copy', 'working_copy'}:
        candidate = parent / f'{stem}-edited{source_suffix}'
    else:
        raise ApiError(f'Unsupported artifact kind: {kind}')

    if not candidate.exists():
        return candidate
    base_stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        trial = candidate.with_name(f'{base_stem}-{index}{suffix}')
        if not trial.exists():
            return trial
    return candidate


def _download_artifact(
    payload: dict[str, Any],
    *,
    kind: str,
    base_url: str,
    out: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    artifact_url = payload.get('download_path')
    if not isinstance(artifact_url, str) or not artifact_url.strip():
        raise ApiError(f'{kind} did not return a download path.')
    destination = _artifact_destination(kind, out=out, out_dir=out_dir)
    return download_to_path(base_url, artifact_url, destination)


def _resolve_pdftoppm(
    *,
    explicit: str | Path | None = None,
    path_entries: list[str | Path] | None = None,
    winget_roots: list[str | Path] | None = None,
) -> Path:
    configured = explicit
    if configured is None:
        try:
            configured = get_settings().pdftoppm_path
        except Exception:
            configured = None
    try:
        result = resolve_pdftoppm(
            explicit=configured,
            path_entries=path_entries,
            winget_roots=winget_roots,
        )
    except PopplerResolutionError as exc:
        raise ApiError(str(exc)) from exc
    if not result.ok or result.path is None:
        raise ApiError(result.detail)
    return result.path


def _positive_int(value: int, *, name: str) -> int:
    if value <= 0:
        raise ApiError(f'{name} must be a positive integer.')
    return value


def _render_pdf_page_to_png(input_pdf: Path, destination: Path, *, page: int, dpi: int) -> Path:
    try:
        pdftoppm = _resolve_pdftoppm()
    except ApiError as exc:
        raise ApiError(f'PDF renderer unavailable: {exc}') from exc
    page = _positive_int(page, name='page')
    dpi = _positive_int(dpi, name='dpi')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hwpx-page-screenshot-') as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        prefix = tmp_dir / 'page'
        try:
            subprocess.run(
                [
                    pdftoppm,
                    '-r',
                    str(dpi),
                    '-f',
                    str(page),
                    '-l',
                    str(page),
                    '-png',
                    str(input_pdf),
                    str(prefix),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b'').decode('utf-8', errors='replace').strip()
            raise ApiError(detail or f'PDF page render failed for page {page}.') from exc
        rendered = sorted(tmp_dir.glob('page-*.png'))
        if not rendered:
            raise ApiError(f'PDF page render did not create an image for page {page}.')
        try:
            if rendered[0].resolve() != destination.resolve():
                shutil.copyfile(rendered[0], destination)
        except FileNotFoundError:
            shutil.copyfile(rendered[0], destination)
    return destination


def _parse_page_range_expression(raw: str) -> list[int]:
    pages: list[int] = []
    seen: set[int] = set()
    for part in str(raw or '').split(','):
        item = part.strip()
        if not item:
            continue
        if '-' in item:
            start_raw, end_raw = item.split('-', 1)
            try:
                start = int(start_raw)
                end = int(end_raw)
            except ValueError as exc:
                raise ApiError(f'Invalid page range item: {item!r}') from exc
            if start <= 0 or end <= 0 or end < start:
                raise ApiError(f'Invalid page range item: {item!r}')
            candidates = range(start, end + 1)
        else:
            try:
                page = int(item)
            except ValueError as exc:
                raise ApiError(f'Invalid page number: {item!r}') from exc
            if page <= 0:
                raise ApiError(f'Invalid page number: {item!r}')
            candidates = [page]
        for page in candidates:
            if page not in seen:
                pages.append(page)
                seen.add(page)
    if not pages:
        raise ApiError('--pages must include at least one positive page number.')
    return pages


def _extract_pdf_page_text(pdf_path: Path, *, page: int) -> str | None:
    pdftotext = shutil.which('pdftotext')
    if not pdftotext:
        return None
    try:
        completed = subprocess.run(
            [pdftotext, '-f', str(page), '-l', str(page), '-layout', str(pdf_path), '-'],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        return None
    return completed.stdout.decode('utf-8', errors='replace')


def _extract_pdf_pages_text(pdf_path: Path) -> list[str] | None:
    pdftotext = shutil.which('pdftotext')
    if not pdftotext:
        return None
    try:
        completed = subprocess.run(
            [pdftotext, '-layout', str(pdf_path), '-'],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        return None
    text = completed.stdout.decode('utf-8', errors='replace')
    pages = text.split('\f')
    if pages and pages[-1] == '':
        pages.pop()
    return pages


def _pdf_page_count(pdf_path: Path) -> tuple[int | None, str | None]:
    pdfinfo = shutil.which('pdfinfo')
    if pdfinfo:
        try:
            completed = subprocess.run(
                [pdfinfo, str(pdf_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError:
            pass
        else:
            output = completed.stdout.decode('utf-8', errors='replace')
            for line in output.splitlines():
                key, separator, value = line.partition(':')
                if separator and key.strip().lower() == 'pages':
                    try:
                        count = int(value.strip().split()[0])
                    except (IndexError, ValueError):
                        break
                    if count >= 0:
                        return count, 'pdfinfo'
                    break
    pages_text = _extract_pdf_pages_text(pdf_path)
    if pages_text is not None:
        return len(pages_text), 'pdftotext-page-breaks'
    return None, None


def _format_page_range_summary(pages: list[int]) -> str:
    if not pages:
        return 'none'
    parts: list[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        parts.append(str(start) if start == previous else f'{start}-{previous}')
        start = previous = page
    parts.append(str(start) if start == previous else f'{start}-{previous}')
    return ','.join(parts)


def _clamp_pages_to_pdf_count(requested: list[int], page_count: int | None) -> tuple[list[int], list[str], dict[str, Any]]:
    metadata = {
        'current_pdf_page_count': page_count,
        'page_count_is_validation_proof': False,
    }
    if page_count is None:
        return list(requested), [], metadata
    requested_summary = _format_page_range_summary(requested)
    effective = [page for page in requested if page <= page_count]
    rendered_summary = _format_page_range_summary(effective)
    if not effective:
        raise ApiError(
            f'requested {requested_summary}; current document has {page_count} pages; '
            'no requested pages are available to render; page count is metadata only, rendered proof remains required.'
        )
    warnings: list[str] = []
    if len(effective) != len(requested):
        warnings.append(
            f'requested {requested_summary}; current document has {page_count} pages; '
            f'rendered {rendered_summary}; page count is metadata only, rendered proof remains required.'
        )
    return effective, warnings, metadata


def _resolve_pages_from_section_text(pdf_path: Path, *, section_anchor: str, until_anchor: str | None = None) -> tuple[list[int], dict[str, Any]]:
    pages_text = _extract_pdf_pages_text(pdf_path)
    if pages_text is None:
        raise ApiError('pdftotext not found or failed; cannot derive section-aware proof pages.')
    anchor = str(section_anchor or '').strip()
    if not anchor:
        raise ApiError('--section-anchor must not be empty.')
    start_index = next((index for index, text in enumerate(pages_text) if anchor in text), None)
    if start_index is None:
        raise ApiError(f'section anchor not found in fresh PDF text: {anchor!r}')
    end_index = start_index
    until_index = None
    until = str(until_anchor or '').strip()
    if until:
        for index in range(start_index + 1, len(pages_text)):
            if until in pages_text[index]:
                until_index = index
                break
        end_index = (until_index - 1) if until_index is not None else len(pages_text) - 1
    pages = list(range(start_index + 1, max(start_index, end_index) + 2))
    return pages, {
        'section_anchor': anchor,
        'section_anchor_page': start_index + 1,
        'until_anchor': until or None,
        'until_anchor_page': (until_index + 1) if until_index is not None else None,
        'page_count_from_text': len(pages_text),
        'derivation': 'pdftotext-page-breaks',
    }


def _maybe_create_contact_sheet(page_pngs: list[Path], out_dir: Path) -> Path | None:
    if not page_pngs:
        return None
    destination = out_dir / 'contact-sheet.png'
    montage = shutil.which('montage')
    if montage:
        try:
            subprocess.run(
                [montage, *[str(path) for path in page_pngs], '-thumbnail', '320x', '-mode', 'Concatenate', '-tile', '2x', str(destination)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError:
            pass
        else:
            if destination.exists():
                return destination

    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    try:
        thumbs = []
        for page_png in page_pngs:
            image = Image.open(page_png).convert('RGB')
            image.thumbnail((320, 452))
            tile = Image.new('RGB', (340, 490), 'white')
            tile.paste(image, ((340 - image.width) // 2, 28))
            ImageDraw.Draw(tile).text((8, 8), page_png.stem, fill=(0, 0, 0))
            thumbs.append(tile)
        columns = 2
        rows = (len(thumbs) + columns - 1) // columns
        sheet = Image.new('RGB', (columns * 340, rows * 490), (240, 240, 240))
        for index, tile in enumerate(thumbs):
            sheet.paste(tile, ((index % columns) * 340, (index // columns) * 490))
        sheet.save(destination)
    except Exception:
        return None
    return destination if destination.exists() else None


def _write_artifact_manifest(kind: str, output_path: Path, *, extra: dict[str, Any] | None = None) -> Path:
    state = load_state()
    manifest_path = output_path.with_name(f'{output_path.stem}.manifest.json')
    source_path = state.get('source_path')
    candidate_identity = _read_candidate_identity()
    source_sha256 = None
    if source_path:
        try:
            source_candidate = Path(str(source_path)).expanduser()
            if source_candidate.is_file():
                source_sha256 = _sha256_file(source_candidate)
        except OSError:
            source_sha256 = None
    output_bytes = None
    output_sha256 = None
    try:
        if output_path.is_file():
            output_bytes = output_path.stat().st_size
            output_sha256 = _sha256_file(output_path)
    except OSError:
        output_bytes = None
        output_sha256 = None
    manifest = {
        'schema_version': 'local-cli/artifact-manifest/v1',
        'kind': kind,
        'source_hwp_path': source_path,
        'source_hwp_sha256': source_sha256,
        'session_id': _state_session_id(),
        'working_copy_id': _state_session_id(),
        'candidate_identity': candidate_identity or None,
        'created_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'output_path': str(output_path),
        'output_bytes': output_bytes,
        'output_sha256': output_sha256,
    }
    if extra:
        manifest.update(extra)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _first_step_result_by_op(raw_payload: dict[str, Any], op: str) -> dict[str, Any]:
    for raw_step in raw_payload.get('steps') if isinstance(raw_payload.get('steps'), list) else []:
        if isinstance(raw_step, dict) and raw_step.get('op') == op and isinstance(raw_step.get('result'), dict):
            return raw_step['result']
    return {}


def _export_pdf_via_bundle(base_url: str) -> tuple[Path, Path]:
    _state_before_export, export_expected_generation, export_expected_session = _state_cas_snapshot()
    spec, raw_payload = _execute_named_bundle(
        base_url,
        'export-proof-range',
        [],
        session_id=export_expected_session,
    )
    export_result = _first_step_result_by_op(raw_payload, 'export_pdf')
    download_path = export_result.get('download_path')
    if not isinstance(download_path, str) or not download_path.strip():
        raise ApiError('bundle-backed export did not return an export download_path.')
    destination = download_to_path(base_url, download_path, _artifact_destination('export'))
    manifest_path = _write_artifact_manifest(
        'export',
        destination,
        extra={
            'server_artifact_path': export_result.get('artifact_path'),
            'download_path': download_path,
            'bundle_name': spec.name,
            'bundle_summary': spec.summary,
            'proof_slice': 'export --bundle-proof',
            'server_primitive': 'export_pdf',
            'route': '/local-cli/command-bundle',
        },
    )
    update_state(
        lambda state: {
            **state,
            'last_export_path': str(destination),
            'last_export_manifest_path': str(manifest_path),
            'last_export_route': 'command-bundle:export_pdf',
        },
        expected_generation=export_expected_generation,
        expected_session_id=export_expected_session,
    )
    return destination, manifest_path


def _record_export_proof_manifest_state(state: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Project export-proof output paths into CLI state for proof-packet collection."""

    updated = dict(state)
    # Export proof is generation-scoped.  Do not merge optional fields from a
    # prior export: an export with no rendered pages must not leave an older
    # screenshot/contact sheet eligible for proof-packet collection.
    for key in (
        'last_export_path',
        'last_export_manifest_path',
        'last_export_proof_manifest_path',
        'last_export_proof_dir',
        'last_export_proof_contact_sheet_path',
        'last_export_proof_page_paths',
        'last_export_proof_generation',
        'last_export_proof_export_sha256',
        'last_export_proof_manifest_sha256',
        'last_export_proof_session_id',
        'last_export_proof_target_identity',
        'last_export_proof_proof_generation',
        'last_export_proof_candidate_identity',
        'last_export_proof_contact_sheet_sha256',
        'last_page_screenshot_path',
        'last_page_screenshot_manifest_path',
        'last_page_screenshot_page',
        'last_page_screenshot_dpi',
    ):
        updated.pop(key, None)
    exported_pdf = manifest.get('exported_pdf_path')
    manifest_path = manifest.get('manifest_path')
    if exported_pdf:
        updated['last_export_path'] = str(exported_pdf)
    export_generation = manifest.get('export_generation')
    if export_generation:
        updated['last_export_proof_generation'] = str(export_generation)
    exported_pdf_sha256 = manifest.get('exported_pdf_sha256')
    if exported_pdf_sha256:
        updated['last_export_proof_export_sha256'] = str(exported_pdf_sha256)
    if manifest_path:
        manifest_path_text = str(Path(str(manifest_path)).expanduser())
        updated['last_export_manifest_path'] = manifest_path_text
        updated['last_export_proof_manifest_path'] = manifest_path_text
        updated['last_export_proof_dir'] = str(Path(manifest_path_text).parent)
        manifest_file = Path(manifest_path_text)
        if manifest_file.is_file():
            updated['last_export_proof_manifest_sha256'] = f'sha256:{_sha256_file(manifest_file)}'
    if manifest.get('session_id'):
        updated['last_export_proof_session_id'] = str(manifest['session_id'])
    if manifest.get('proof_generation'):
        updated['last_export_proof_proof_generation'] = str(manifest['proof_generation'])
    if isinstance(manifest.get('candidate_identity'), dict):
        updated['last_export_proof_candidate_identity'] = dict(manifest['candidate_identity'])
    if isinstance(manifest.get('target_identity'), dict):
        updated['last_export_proof_target_identity'] = dict(manifest['target_identity'])
    contact_sheet = manifest.get('contact_sheet_path')
    if contact_sheet:
        updated['last_export_proof_contact_sheet_path'] = str(contact_sheet)
        contact_sheet_file = Path(str(contact_sheet)).expanduser()
        if contact_sheet_file.is_file():
            updated['last_export_proof_contact_sheet_sha256'] = f'sha256:{_sha256_file(contact_sheet_file)}'
    page_paths: list[str] = []
    pages = manifest.get('pages') if isinstance(manifest.get('pages'), list) else []
    for item in pages:
        if isinstance(item, dict) and item.get('png_path'):
            page_paths.append(str(item['png_path']))
    if page_paths:
        updated['last_export_proof_page_paths'] = page_paths
    return updated


def _render_export_proof_manifest(
    *,
    base_url: str,
    raw_payload: dict[str, Any],
    pages: list[int] | None,
    dpi: int,
    out_dir: Path,
    anchors: list[str],
    section_anchor: str | None = None,
    until_anchor: str | None = None,
    all_pages: bool = False,
    contact_sheet_requested: bool = False,
    fresh_session: dict[str, Any] | None = None,
    target_identity: dict[str, Any] | None = None,
    proof_generation: str | None = None,
) -> dict[str, Any]:
    if dpi <= 0:
        raise ApiError('--dpi must be a positive integer.')
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    export_result = _first_step_result_by_op(raw_payload, 'export_pdf')
    download_path = export_result.get('download_path')
    if not isinstance(download_path, str) or not download_path.strip():
        raise ApiError('export-proof-range bundle did not return an export download_path.')
    pdf_path = download_to_path(base_url, download_path, out_dir / 'source.pdf')
    exported_pdf_sha256 = f'sha256:{_sha256_file(pdf_path)}'
    export_generation = f'local-cli-export/v1:{exported_pdf_sha256}'
    section_derivation: dict[str, Any] | None = None
    current_pdf_page_count, page_count_source = _pdf_page_count(pdf_path)
    if pages is None:
        if all_pages:
            if current_pdf_page_count is None:
                raise ApiError('Could not determine current PDF page count for --all-pages; rendered proof remains required.')
            pages = list(range(1, current_pdf_page_count + 1))
            section_derivation = {
                'derivation': 'all-pages-from-current-pdf-page-count',
                'page_count': current_pdf_page_count,
                'page_count_source': page_count_source,
                'page_count_is_validation_proof': False,
            }
        elif not section_anchor:
            raise ApiError('export-proof-range requires --pages, --section-anchor, or --all-pages.')
        else:
            pages, section_derivation = _resolve_pages_from_section_text(
                pdf_path,
                section_anchor=section_anchor,
                until_anchor=until_anchor,
            )
    pages_requested = list(pages)
    pages_effective, warnings, page_count_metadata = _clamp_pages_to_pdf_count(pages_requested, current_pdf_page_count)
    rendered_pages: list[dict[str, Any]] = []
    png_paths: list[Path] = []
    text_extraction_available = shutil.which('pdftotext') is not None
    for page in pages_effective:
        png_path = out_dir / f'page-{page:03d}.png'
        _render_pdf_page_to_png(pdf_path, png_path, page=page, dpi=dpi)
        png_paths.append(png_path)
        page_text = _extract_pdf_page_text(pdf_path, page=page)
        token_hits = {token: (token in page_text if page_text is not None else None) for token in anchors}
        rendered_pages.append(
            {
                'page': page,
                'png_path': str(png_path),
                'png_sha256': f'sha256:{_sha256_file(png_path)}',
                'token_hits': token_hits,
                'text_extraction_available': page_text is not None,
            }
        )
    contact_sheet = _maybe_create_contact_sheet(png_paths, out_dir) if (contact_sheet_requested or section_anchor) else None
    state = load_state()
    candidate_identity = _read_candidate_identity()
    manifest = {
        'schema_version': 'local-cli/export-proof-range/v1',
        'ok': True,
        'source_hwp_path': state.get('source_path'),
        'candidate_identity': candidate_identity or None,
        'session_id': _state_session_id(),
        'created_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'pages_requested': pages_requested,
        'pages_effective': pages_effective,
        'pages_rendered': pages_effective,
        'pages_requested_summary': _format_page_range_summary(pages_requested),
        'pages_rendered_summary': _format_page_range_summary(pages_effective),
        'section_derivation': section_derivation,
        'dpi': dpi,
        'exported_pdf_path': str(pdf_path),
        'exported_pdf_sha256': exported_pdf_sha256,
        'export_generation': export_generation,
        'current_pdf_page_count': current_pdf_page_count,
        'page_count_source': page_count_source,
        'page_count_is_validation_proof': False,
        'warnings': warnings,
        'server_export_artifact_path': export_result.get('artifact_path'),
        'contact_sheet_path': str(contact_sheet) if contact_sheet else None,
        'contact_sheet_sha256': f'sha256:{_sha256_file(contact_sheet)}' if contact_sheet else None,
        'anchors': anchors,
        'target_identity': {
            'section_anchor': section_anchor,
            'until_anchor': until_anchor,
            'pages_effective': pages_effective,
            'proof_match_identity': dict(target_identity) if isinstance(target_identity, dict) else None,
        },
        'proof_generation': proof_generation,
        'fresh_session': fresh_session or {'requested': False},
        'text_extraction_available': text_extraction_available,
        'pages': rendered_pages,
        'bundle_summary': raw_payload.get('summary'),
    }
    manifest.update(page_count_metadata)
    manifest_path = out_dir / 'manifest.json'
    manifest['manifest_path'] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def _run_qa_profile(
    *,
    base_url: str,
    raw_payload: dict[str, Any],
    out_dir: Path,
    section_anchor: str | None,
    until_anchor: str | None,
    forbid: list[str],
    require: list[str],
    after_anchor_forbid: list[str],
    source_hash: str | None,
) -> dict[str, Any]:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    export_result = _first_step_result_by_op(raw_payload, 'export_pdf')
    download_path = export_result.get('download_path')
    if not isinstance(download_path, str) or not download_path.strip():
        raise ApiError('qa-profile bundle did not return an export download_path.')
    pdf_path = download_to_path(base_url, download_path, out_dir / 'qa-source.pdf')
    pages_text = _extract_pdf_pages_text(pdf_path)
    if pages_text is None:
        raise ApiError('pdftotext not found or failed; qa-profile requires fresh PDF text extraction.')
    all_text = '\n\f\n'.join(pages_text)
    scoped_text = all_text
    section_derivation = None
    if section_anchor:
        pages, section_derivation = _resolve_pages_from_section_text(
            pdf_path,
            section_anchor=section_anchor,
            until_anchor=until_anchor,
        )
        scoped_text = '\n'.join(pages_text[page - 1] for page in pages if 1 <= page <= len(pages_text))

    failures: list[str] = []
    forbidden_hits = {token: (token in scoped_text) for token in forbid}
    required_hits = {token: (token in scoped_text) for token in require}
    for token, hit in forbidden_hits.items():
        if hit:
            failures.append(f'forbidden token present: {token}')
    for token, hit in required_hits.items():
        if not hit:
            failures.append(f'required token missing: {token}')

    after_checks: list[dict[str, Any]] = []
    for raw_pair in after_anchor_forbid:
        if '::' not in raw_pair:
            failures.append(f'invalid after-anchor-forbid pair (expected ANCHOR::TOKEN): {raw_pair}')
            continue
        anchor, token = raw_pair.split('::', 1)
        anchor = anchor.strip()
        token = token.strip()
        anchor_index = all_text.find(anchor)
        token_after = False if anchor_index < 0 else token in all_text[anchor_index + len(anchor):]
        after_checks.append({'anchor': anchor, 'token': token, 'anchor_found': anchor_index >= 0, 'token_after_anchor': token_after})
        if anchor_index < 0:
            failures.append(f'after-anchor anchor missing: {anchor}')
        elif token_after:
            failures.append(f'token appears after anchor {anchor!r}: {token}')

    state = load_state()
    source_path_raw = state.get('source_path')
    source_sha256 = None
    if source_path_raw and Path(str(source_path_raw)).expanduser().exists():
        source_sha256 = _sha256_file(Path(str(source_path_raw)).expanduser())
    if source_hash and source_sha256:
        expected = source_hash.strip().lower()
        # This guard is intentionally conservative: matching the original/source
        # hash is a freshness risk for an intended edited candidate.
        if source_sha256.lower() == expected:
            failures.append('source SHA256 equals the provided source-hash guard; candidate may be unchanged from source')

    manifest = {
        'schema_version': 'local-cli/qa-profile/v1',
        'ok': not failures,
        'created_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'source_hwp_path': source_path_raw,
        'source_sha256': source_sha256,
        'source_hash_guard': source_hash,
        'session_id': _state_session_id(),
        'exported_pdf_path': str(pdf_path),
        'server_export_artifact_path': export_result.get('artifact_path'),
        'text_extraction': {'available': True, 'page_count': len(pages_text), 'fresh_pdf': True},
        'section_derivation': section_derivation,
        'forbidden_hits': forbidden_hits,
        'required_hits': required_hits,
        'after_anchor_forbid': after_checks,
        'failures': failures,
        'bundle_summary': raw_payload.get('summary'),
    }
    manifest_path = out_dir / 'qa-profile.json'
    manifest['manifest_path'] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def _run_section_control_delete_exact(
    *,
    base_url: str,
    bundle_args: list[str],
    expected_page: int,
    proof_out_dir: Path,
    dpi: int,
) -> dict[str, Any]:
    proof_out_dir = proof_out_dir.expanduser().resolve()
    proof_out_dir.mkdir(parents=True, exist_ok=True)
    pages_expr = str(expected_page)

    _before_spec, before_payload = _execute_named_bundle(
        base_url,
        'export-proof-range',
        ['--pages', pages_expr, '--dpi', str(dpi), '--out-dir', str(proof_out_dir / 'before')],
    )
    before_manifest = _render_export_proof_manifest(
        base_url=base_url,
        raw_payload=before_payload,
        pages=[expected_page],
        dpi=dpi,
        out_dir=proof_out_dir / 'before',
        anchors=[],
    )

    spec, mutation_payload = _execute_named_bundle(base_url, 'section-control-delete-exact', bundle_args)
    mutation_ok = bool(mutation_payload.get('ok'))
    after_manifest = None
    if mutation_ok:
        _after_spec, after_payload = _execute_named_bundle(
            base_url,
            'export-proof-range',
            ['--pages', pages_expr, '--dpi', str(dpi), '--out-dir', str(proof_out_dir / 'after')],
        )
        after_manifest = _render_export_proof_manifest(
            base_url=base_url,
            raw_payload=after_payload,
            pages=[expected_page],
            dpi=dpi,
            out_dir=proof_out_dir / 'after',
            anchors=[],
        )

    manifest = {
        'schema_version': 'local-cli/one-control-mutation-manifest/v1',
        'ok': mutation_ok,
        'created_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'source_hwp_path': load_state().get('source_path'),
        'session_id': _state_session_id(),
        'bundle': spec.debug_payload(),
        'expected_page': expected_page,
        'dpi': dpi,
        'before_render_manifest': before_manifest,
        'mutation_payload': mutation_payload,
        'after_render_manifest': after_manifest,
        'rollback_or_discard': 'Discard this disposable/working copy if rendered proof is not acceptable; originals are not mutated by the CLI open path.',
    }
    manifest_path = proof_out_dir / 'one-control-mutation-manifest.json'
    manifest['manifest_path'] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def _run_section_control_move_resize_exact(
    *,
    base_url: str,
    bundle_args: list[str],
    expected_page: int,
    proof_out_dir: Path,
    dpi: int,
) -> dict[str, Any]:
    proof_out_dir = proof_out_dir.expanduser().resolve()
    proof_out_dir.mkdir(parents=True, exist_ok=True)
    pages_expr = str(expected_page)

    _before_spec, before_payload = _execute_named_bundle(
        base_url,
        'export-proof-range',
        ['--pages', pages_expr, '--dpi', str(dpi), '--out-dir', str(proof_out_dir / 'before')],
    )
    before_manifest = _render_export_proof_manifest(
        base_url=base_url,
        raw_payload=before_payload,
        pages=[expected_page],
        dpi=dpi,
        out_dir=proof_out_dir / 'before',
        anchors=[],
    )

    spec, mutation_payload = _execute_named_bundle(base_url, 'section-control-move-resize-exact', bundle_args)
    mutation_ok = bool(mutation_payload.get('ok'))
    after_manifest = None
    if mutation_ok:
        _after_spec, after_payload = _execute_named_bundle(
            base_url,
            'export-proof-range',
            ['--pages', pages_expr, '--dpi', str(dpi), '--out-dir', str(proof_out_dir / 'after')],
        )
        after_manifest = _render_export_proof_manifest(
            base_url=base_url,
            raw_payload=after_payload,
            pages=[expected_page],
            dpi=dpi,
            out_dir=proof_out_dir / 'after',
            anchors=[],
        )

    manifest = {
        'schema_version': 'local-cli/one-control-layout-mutation-manifest/v1',
        'ok': mutation_ok,
        'created_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'source_hwp_path': load_state().get('source_path'),
        'session_id': _state_session_id(),
        'bundle': spec.debug_payload(),
        'expected_page': expected_page,
        'dpi': dpi,
        'before_render_manifest': before_manifest,
        'mutation_payload': mutation_payload,
        'after_render_manifest': after_manifest,
        'rollback_or_discard': 'Discard this disposable/working copy if rendered proof is not acceptable; originals are not mutated by the CLI open path.',
    }
    manifest_path = proof_out_dir / 'one-control-layout-mutation-manifest.json'
    manifest['manifest_path'] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def _page_screenshot(
    *,
    base_url: str,
    page: int,
    dpi: int,
    out: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    # Page proof is deliberately separate from live screenshot: it exports the
    # working copy to PDF and renders a document page, so it cannot replace the
    # live Hancom full-frame/caret proof returned by `hwpx screenshot`.
    page = _positive_int(page, name='page')
    dpi = _positive_int(dpi, name='dpi')
    _state_before_screenshot, screenshot_expected_generation, screenshot_expected_session = _state_cas_snapshot()
    payload = post_json(base_url, '/local-cli/export', {'session_id': screenshot_expected_session})
    artifact_url = payload.get('download_path')
    if not isinstance(artifact_url, str) or not artifact_url.strip():
        raise ApiError('page screenshot export did not return a download path.')
    with tempfile.TemporaryDirectory(prefix='hwpx-page-screenshot-pdf-') as tmp_dir_raw:
        tmp_pdf = Path(tmp_dir_raw) / 'source.pdf'
        download_to_path(base_url, artifact_url, tmp_pdf)
        destination = _artifact_destination('page-screenshot', page=page, out=out, out_dir=out_dir)
        _render_pdf_page_to_png(tmp_pdf, destination, page=page, dpi=dpi)
    screenshot_manifest_path = _write_artifact_manifest(
        'page-screenshot',
        destination,
        extra={'page': page, 'requested_page': page, 'dpi': dpi},
    )
    update_state(
        lambda state: {
            **state,
            'last_page_screenshot_path': str(destination),
            'last_page_screenshot_page': page,
            'last_page_screenshot_dpi': dpi,
            'last_page_screenshot_manifest_path': str(screenshot_manifest_path),
        },
        expected_generation=screenshot_expected_generation,
        expected_session_id=screenshot_expected_session,
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base_url = _resolve_base_url(args.base_url)

    try:
        if args.command == 'help':
            if args.topic == 'commands':
                _print_command_status()
            else:
                _print_workflow_help()
            return 0

        if args.command == 'command-status':
            _print_command_status()
            return 0

        if args.command == 'command-reconcile':
            payload = post_json(
                base_url,
                '/local-cli/command-reconcile',
                {
                    'command_id': args.command_id,
                    'session_id': args.session_id or _state_session_id(),
                },
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"command reconciliation: {payload.get('reconciliation', 'unknown')}")
                print(f"command_id: {args.command_id}")
                print(f"session_id: {payload.get('session_id') or args.session_id or _state_session_id() or 'unknown'}")
            return 0

        if args.command == 'safe-schema':
            payload = build_safe_agent_schema(build_command_status(parser))
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print('safe-schema: non-mutating commands only')
                for group_name, group in payload.get('operation_groups', {}).items():
                    commands = ', '.join(str(command) for command in group.get('commands', []))
                    print(f'{group_name}: {commands}')
                print('mutation allowed: no')
                print('direct package mutation allowed: no')
            return 0

        if args.command == 'open':
            if not args.file.exists() or not args.file.is_file():
                raise ApiError(f'Local file not found: {args.file}')
            state_before_open = load_state()
            open_expected_generation = int(state_before_open.get('state_generation', 0))
            open_expected_session = str(state_before_open.get('session_id') or '').strip() or None
            payload = post_file(base_url, '/local-cli/open', field_name='file', file_path=args.file)
            session_id = payload.get('session_id')
            source_filename = payload.get('source_filename') or args.file.name
            candidate_identity = _read_candidate_identity()
            update_state(
                lambda _state: {
                    'base_url': base_url,
                    'session_id': session_id,
                    'source_filename': source_filename,
                    'source_path': str(args.file.resolve()),
                    **candidate_identity,
                },
                expected_generation=open_expected_generation,
                expected_session_id=open_expected_session,
            )
            if args.json:
                print(json.dumps({
                    'schema_version': 'local-cli/lifecycle/v1',
                    'ok': bool(payload.get('ok', True)),
                    'command': 'open',
                    'base_url': base_url,
                    'managed_fixture': str(args.file.resolve()),
                    'source_path': str(args.file.resolve()),
                    'source_filename': source_filename,
                    'session_id': session_id,
                    'working_copy_id': payload.get('working_copy_id') or session_id,
                    'live_session_bound': True,
                    **candidate_identity,
                    'response': payload,
                }, ensure_ascii=False, indent=2))
                return 0
            _print_lifecycle_result(
                where=f'source={args.file.resolve()}; session={session_id or "unknown"}; active document={source_filename}',
                how='direct /local-cli/open upload into a server-managed working copy',
                changed='live session opened/bound; original source file untouched',
                proof='none yet; run `hwpx where` and rendered proof commands after targeting/editing',
                next_step='run `hwpx status`, then `hwpx find`/`hwpx where`/`hwpx select` before editing.',
            )
            return 0

        if args.command == 'status':
            payload = get_json(base_url, '/local-cli/status')
            payload.update(_probe_command_bundle_route(base_url))
            payload = _with_status_identity(payload)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_status(payload)
            return 0

        if args.command in {'session-health', 'bundle-health'}:
            _print_session_health(base_url)
            return 0

        if args.command == 'state':
            _print_state()
            return 0

        if args.command == 'proof-packet':
            try:
                manifest = build_proof_packet(out_dir=args.out_dir, state=load_state(), state_path=default_state_path())
            except ProofPacketError as exc:
                raise ApiError(str(exc)) from exc
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
            else:
                print(f"proof packet: {manifest.get('packet_dir')}")
                print(f"manifest: {manifest.get('manifest_path')}")
            return 0

        if args.command == 'native-border-readback':
            try:
                manifest = _record_native_border_readback(
                    pre_quit_path=args.pre_quit,
                    persisted_path=args.persisted,
                    target_identity_path=args.target_identity,
                    destination=args.out,
                )
            except ProofPacketError as exc:
                raise ApiError(str(exc)) from exc
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
            else:
                print(f"native border readback: {manifest.get('artifact_path')}")
            return 0

        if args.command == 'reset-state':
            state_path = default_state_path()
            clear_state(state_path)
            print(state_path)
            return 0

        if args.command == 'find':
            state_before_find, find_expected_generation, find_expected_session = _state_cas_snapshot()
            request = {
                'query': args.text,
                'session_id': find_expected_session,
                'around': args.around,
                'with_page': args.with_page or args.proof_match is not None,
                'proof_match': args.proof_match,
            }
            payload = post_json(base_url, '/local-cli/find', request)
            state = load_state()
            if args.proof_match is not None:
                proof = payload.get('proof_match') if isinstance(payload.get('proof_match'), dict) else None
                page_value = proof.get('page') if proof else None
                if not isinstance(page_value, int) or page_value <= 0:
                    page_value = proof.get('page_candidate') if proof else None
                if not isinstance(page_value, int) or page_value <= 0:
                    raise ApiError(
                        f'proof match {args.proof_match} has no usable page evidence; rerun find with --with-page or use a native page proof.'
                    )
                proof_out_dir = (args.proof_out_dir or _find_proof_out_dir(state, match_number=args.proof_match, page=page_value)).expanduser()
                bundle_args = [
                    '--pages', str(page_value),
                    '--dpi', str(args.dpi),
                    '--out-dir', str(proof_out_dir),
                ]
                if args.contact_sheet:
                    bundle_args.append('--contact-sheet')
                bundle_args.extend(['--anchor', args.text])
                _spec, proof_payload = _execute_named_bundle(base_url, 'export-proof-range', bundle_args)
                proof_manifest = _render_export_proof_manifest(
                    base_url=base_url,
                    raw_payload=proof_payload,
                    pages=[page_value],
                    dpi=args.dpi,
                    out_dir=proof_out_dir,
                    anchors=[args.text],
                    contact_sheet_requested=bool(args.contact_sheet),
                    target_identity=(proof.get('identity') if isinstance(proof, dict) and isinstance(proof.get('identity'), dict) else None),
                    proof_generation=(str(proof.get('proof_generation')) if isinstance(proof, dict) and proof.get('proof_generation') else None),
                )
            if args.proof_match is not None:
                update_state(
                    lambda current: _record_export_proof_manifest_state(
                        {**current, 'last_find_query': args.text},
                        proof_manifest,
                    ),
                    expected_generation=find_expected_generation,
                    expected_session_id=find_expected_session,
                )
            else:
                update_state(
                    lambda current: {**current, 'last_find_query': args.text},
                    expected_generation=find_expected_generation,
                    expected_session_id=find_expected_session,
                )
            if args.json:
                output = dict(payload)
                if args.proof_match is not None:
                    output['proof_manifest'] = proof_manifest
                print(json.dumps(output, ensure_ascii=False, indent=2))
            else:
                _print_matches(payload)
                if args.proof_match is not None:
                    print(f"proof match: {args.proof_match}")
                    print(f"proof page: {page_value}")
                    print(f"manifest: {proof_manifest.get('manifest_path')}")
                    print(f"hwpx proof-packet --out-dir {proof_manifest.get('out_dir') or proof_out_dir}")
            return 0

        if args.command == 'info':
            payload = post_json(base_url, '/local-cli/info', {'target': args.target, 'session_id': _state_session_id()})
            _print_info(payload)
            return 0

        if args.command == 'move':
            payload = post_json(base_url, '/local-cli/move', {'target': args.target, 'session_id': _state_session_id()})
            _print_command_payload(payload)
            return 0

        if args.command == 'select':
            payload = post_json(base_url, '/local-cli/select', {'target': args.target, 'session_id': _state_session_id()})
            _print_command_payload(payload)
            selected_text = str(payload.get('selected_text') or '').strip()
            if selected_text and payload.get('active_selection_verified') is True and payload.get('safe_for_type') is True:
                print(f'selected text: {selected_text}')
            elif selected_text and payload.get('active_selection_verified') is True:
                print(f'active selection proof only: {selected_text}')
            elif selected_text:
                print(f'cached selected-text proof only: {selected_text}')
            return 0

        if args.command == 'cell':
            payload = post_json(base_url, '/local-cli/cell', {'session_id': _state_session_id()})
            _print_command_payload(payload)
            return 0

        if args.command == 'cellmove':
            payload = post_json(
                base_url,
                '/local-cli/cellmove',
                {'direction': args.direction, 'count': args.count, 'session_id': _state_session_id()},
            )
            _print_command_payload(payload)
            return 0

        if args.command == 'cursormove':
            payload = post_json(
                base_url,
                '/local-cli/cursormove',
                {'direction': args.direction, 'count': args.count, 'session_id': _state_session_id()},
            )
            _print_command_payload(payload)
            return 0

        if args.command == 'where':
            try:
                _spec, payload = _execute_named_bundle(base_url, 'where')
            except ApiError as exc:
                if exc.status_code not in {404, 405}:
                    raise
                # Temporary migration fallback only for a not-yet-reloaded live API.
                # This keeps `hwpx where` usable while preserving command-bundle as
                # the target public pipeline once `/local-cli/command-bundle` is live.
                print(
                    'warning: /local-cli/command-bundle unavailable; temporary legacy /local-cli/where fallback used',
                    file=sys.stderr,
                )
                payload = post_json(base_url, '/local-cli/where', {'session_id': _state_session_id()})
                if args.json:
                    print(json.dumps(_with_lifecycle_identity(payload, base_url=base_url, command='where'), ensure_ascii=False, indent=2))
                else:
                    _print_where(payload)
                return 0
            if args.json:
                normalized = normalize_command_bundle(payload)
                print(json.dumps(_with_lifecycle_identity(normalized, base_url=base_url, command='where'), ensure_ascii=False, indent=2))
            else:
                print(format_where_bundle_human(payload))
            return 0

        if args.command == 'context':
            _spec, payload = _execute_named_bundle(base_url, 'context')
            if args.json:
                print(json.dumps(summarize_context(payload), ensure_ascii=False, indent=2))
            else:
                print(format_context_human(payload))
            return 0

        if args.command == 'readback-diff':
            try:
                source_manifest = load_readback_manifest(args.source_manifest.expanduser())
                candidate_manifest = load_readback_manifest(args.candidate_manifest.expanduser())
            except (OSError, json.JSONDecodeError) as exc:
                raise ApiError(f'readback-diff manifest load failed: {exc}') from exc
            artifact_dir = args.artifact_dir.expanduser() if args.artifact_dir else None
            payload = summarize_readback_diff(
                source_manifest,
                candidate_manifest,
                artifact_dir=artifact_dir,
                max_issues=args.max_issues,
            )
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(format_readback_diff_human(payload))
            return 0

        if args.command in {'static-info', 'static-read'}:
            artifact_dir = args.artifact_dir.expanduser() if args.artifact_dir else None
            payload = inspect_static(
                args.file.expanduser(),
                artifact_dir=artifact_dir,
                extract_images=bool(args.extract_images),
                engine=args.engine,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == 'static-compare':
            payload = compare_static(
                args.source_file.expanduser(),
                args.candidate_file.expanduser(),
                artifact_dir=args.artifact_dir.expanduser() if args.artifact_dir else None,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == 'static-render':
            payload = quick_render_static(
                args.file.expanduser(),
                output_path=args.output.expanduser(),
                render_format=args.format,
                artifact_dir=args.artifact_dir.expanduser() if args.artifact_dir else None,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == 'field-fill-plan':
            try:
                replacements = load_replacements(args.replacements_json.expanduser())
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ApiError(f'field-fill-plan replacements load failed: {exc}') from exc
            payload = build_field_fill_plan(
                args.file.expanduser(),
                replacements,
                artifact_dir=args.artifact_dir.expanduser() if args.artifact_dir else None,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == 'output-format-policy':
            payload = output_format_policy(args.input_file.expanduser(), args.output_file.expanduser(), operation=args.operation)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == 'gate-verdict':
            try:
                source_manifest = load_gate_manifest(args.source_manifest.expanduser())
                candidate_manifest = load_gate_manifest(args.candidate_manifest.expanduser())
                static_supplement = load_gate_manifest(args.static_supplement.expanduser()) if args.static_supplement else None
            except (OSError, json.JSONDecodeError) as exc:
                raise ApiError(f'gate-verdict manifest load failed: {exc}') from exc
            payload = summarize_gate_verdict(
                source_manifest,
                candidate_manifest,
                planned_mutations=args.planned_mutations,
                require_tokens=args.require,
                forbid_tokens=args.forbid,
                render_manifest=args.render_manifest.expanduser() if args.render_manifest else None,
                static_supplement=static_supplement,
                allow_count_drift=args.allow_count_drift,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command in {'readback', 'read-context', 'read-manifest'}:
            bundle_args: list[str] = ['--scope', args.scope]
            if args.page_from is not None:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.max_blocks:
                bundle_args.extend(['--max-blocks', str(args.max_blocks)])
            if args.max_table_cells:
                bundle_args.extend(['--max-table-cells', str(args.max_table_cells)])
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            _spec, payload = _execute_named_bundle(base_url, args.command, bundle_args)
            if args.json:
                print(json.dumps(summarize_readback(payload), ensure_ascii=False, indent=2))
            else:
                print(format_readback_human(payload))
            return 0

        if args.command == 'typography-overview':
            bundle_args = ['--scope', args.scope]
            if args.max_samples:
                bundle_args.extend(['--max-samples', str(args.max_samples)])
            if args.max_sections:
                bundle_args.extend(['--max-sections', str(args.max_sections)])
            if args.max_styles:
                bundle_args.extend(['--max-styles', str(args.max_styles)])
            _spec, payload = _execute_named_bundle(base_url, 'typography-overview', bundle_args)
            if args.json:
                print(json.dumps(summarize_typography_overview(payload), ensure_ascii=False, indent=2))
            else:
                print(format_typography_overview_human(payload))
            return 0

        if args.command == 'selection-proof':
            _spec, payload = _execute_named_bundle(base_url, 'selection-proof')
            if args.json:
                print(json.dumps(summarize_selection_proof(payload), ensure_ascii=False, indent=2))
            else:
                print(format_selection_proof_human(payload))
            return 0

        if args.command == 'selected-text-proof':
            bundle_args = ['--clear-selection'] if args.clear_selection else []
            _spec, payload = _execute_named_bundle(base_url, 'selected-text-proof', bundle_args)
            if args.json:
                _print_bundle_payload(payload, json_output=True)
            else:
                print(format_selected_text_proof_human(payload))
            return 0

        if args.command == 'section-control-inventory':
            bundle_args: list[str] = []
            if args.section_anchor:
                bundle_args.extend(['--section-anchor', args.section_anchor])
            else:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.around:
                bundle_args.extend(['--around', args.around])
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            _spec, payload = _execute_named_bundle(base_url, 'section-control-inventory', bundle_args)
            if args.json:
                print(json.dumps(summarize_section_control_inventory(payload), ensure_ascii=False, indent=2))
            else:
                print(format_section_control_inventory_human(payload))
            return 0

        if args.command == 'section-table-frame-inventory':
            bundle_args: list[str] = []
            if args.section_anchor:
                bundle_args.extend(['--section-anchor', args.section_anchor])
            else:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.around:
                bundle_args.extend(['--around', args.around])
            if args.target_id:
                bundle_args.extend(['--target-id', args.target_id])
            if args.expected_hash:
                bundle_args.extend(['--expected-hash', args.expected_hash])
            if args.expected_page is not None:
                bundle_args.extend(['--expected-page', str(args.expected_page)])
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            _spec, payload = _execute_named_bundle(base_url, 'section-table-frame-inventory', bundle_args)
            if args.json:
                print(json.dumps(summarize_section_table_frame_inventory(payload), ensure_ascii=False, indent=2))
            else:
                print(format_section_table_frame_inventory_human(payload))
            return 0

        if args.command == 'table-cell-structure-exact':
            bundle_args = ['--target-id', args.target_id, '--expected-hash', args.expected_hash, '--expected-page', str(args.expected_page)]
            if args.section_anchor:
                bundle_args.extend(['--section-anchor', args.section_anchor])
            else:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.around:
                bundle_args.extend(['--around', args.around])
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            _spec, payload = _execute_named_bundle(base_url, 'table-cell-structure-exact', bundle_args)
            if args.json:
                print(json.dumps(summarize_table_cell_structure(payload), ensure_ascii=False, indent=2))
            else:
                print(format_table_cell_structure_human(payload))
            return 0

        if args.command == 'section-frame-fill':
            bundle_args = ['--target-id', args.target_id, '--text-file', str(args.text_file), '--style-source', args.style_source]
            if args.section_anchor:
                bundle_args.extend(['--section-anchor', args.section_anchor])
            else:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.around:
                bundle_args.extend(['--around', args.around])
            if args.expect_blank:
                bundle_args.append('--expect-blank')
            if args.expect_token:
                bundle_args.extend(['--expect-token', args.expect_token])
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            spec = build_named_bundle('section-frame-fill', bundle_args)
            print('수정 안 됨 / no mutation performed')
            print(dumps_debug_payload(spec))
            return 0

        if args.command == 'section-graphic-remove-or-hide':
            bundle_args = ['--target-id', args.target_id, '--expected-hash', args.expected_hash]
            if args.section_anchor:
                bundle_args.extend(['--section-anchor', args.section_anchor])
            else:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.around:
                bundle_args.extend(['--around', args.around])
            if args.expected_page is not None:
                bundle_args.extend(['--expected-page', str(args.expected_page)])
            bundle_args.append('--confirm-remove' if args.confirm_remove else '--hide-only')
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            spec = build_named_bundle('section-graphic-remove-or-hide', bundle_args)
            print('수정 안 됨 / no mutation performed')
            print(dumps_debug_payload(spec))
            return 0

        if args.command == 'section-control-delete-exact':
            bundle_args = ['--target-id', args.target_id, '--expected-hash', args.expected_hash, '--expected-page', str(args.expected_page)]
            if args.section_anchor:
                bundle_args.extend(['--section-anchor', args.section_anchor])
            else:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.around:
                bundle_args.extend(['--around', args.around])
            if args.confirm_remove:
                bundle_args.append('--confirm-remove')
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            manifest = _run_section_control_delete_exact(
                base_url=base_url,
                bundle_args=bundle_args,
                expected_page=args.expected_page,
                proof_out_dir=args.proof_out_dir,
                dpi=args.dpi,
            )
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
            else:
                print(f"mutation: {'PASS' if manifest.get('ok') else 'FAIL'}")
                print(f"manifest: {manifest.get('manifest_path')}")
                print(f"before: {manifest.get('before_render_manifest', {}).get('manifest_path')}")
                after = manifest.get('after_render_manifest') or {}
                if after:
                    print(f"after: {after.get('manifest_path')}")
            return 0

        if args.command == 'section-control-move-resize-exact':
            bundle_args = ['--target-id', args.target_id, '--expected-hash', args.expected_hash, '--expected-page', str(args.expected_page)]
            if args.section_anchor:
                bundle_args.extend(['--section-anchor', args.section_anchor])
            else:
                bundle_args.extend(['--page-from', str(args.page_from)])
            if args.page_to is not None:
                bundle_args.extend(['--page-to', str(args.page_to)])
            if args.around:
                bundle_args.extend(['--around', args.around])
            if args.scale_percent is not None:
                bundle_args.extend(['--scale-percent', str(args.scale_percent)])
            if args.move_dx_mm:
                bundle_args.extend(['--move-dx-mm', str(args.move_dx_mm)])
            if args.move_dy_mm:
                bundle_args.extend(['--move-dy-mm', str(args.move_dy_mm)])
            if args.confirm_layout:
                bundle_args.append('--confirm-layout')
            if args.max_controls:
                bundle_args.extend(['--max-controls', str(args.max_controls)])
            manifest = _run_section_control_move_resize_exact(
                base_url=base_url,
                bundle_args=bundle_args,
                expected_page=args.expected_page,
                proof_out_dir=args.proof_out_dir,
                dpi=args.dpi,
            )
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
            else:
                print(f"mutation: {'PASS' if manifest.get('ok') else 'FAIL'}")
                print(f"manifest: {manifest.get('manifest_path')}")
                print(f"before: {manifest.get('before_render_manifest', {}).get('manifest_path')}")
                after = manifest.get('after_render_manifest') or {}
                if after:
                    print(f"after: {after.get('manifest_path')}")
            return 0

        if args.command == 'export-proof-range':
            proof_modes = sum(1 for value in (args.pages, args.section_anchor, args.all_pages) if value)
            if proof_modes != 1:
                raise ApiError('export-proof-range requires exactly one of --pages, --section-anchor, or --all-pages.')
            pages = _parse_page_range_expression(args.pages) if args.pages else None
            fresh_session = _reopen_fresh_session(base_url, args.source_hwp) if args.fresh_session else {'requested': False}
            if args.pages:
                bundle_args = ['--pages', args.pages, '--dpi', str(args.dpi), '--out-dir', str(args.out_dir)]
            elif args.section_anchor:
                bundle_args = ['--section-anchor', args.section_anchor, '--dpi', str(args.dpi), '--out-dir', str(args.out_dir)]
            else:
                bundle_args = ['--all-pages', '--dpi', str(args.dpi), '--out-dir', str(args.out_dir)]
            if args.until_anchor:
                bundle_args.extend(['--until-anchor', args.until_anchor])
            if args.fresh_session:
                bundle_args.append('--fresh-session')
            if args.source_hwp:
                bundle_args.extend(['--source-hwp', str(args.source_hwp)])
            if args.contact_sheet:
                bundle_args.append('--contact-sheet')
            for token in args.anchor or []:
                bundle_args.extend(['--anchor', token])
            _spec, payload = _execute_named_bundle(base_url, 'export-proof-range', bundle_args)
            manifest = _render_export_proof_manifest(
                base_url=base_url,
                raw_payload=payload,
                pages=pages,
                dpi=args.dpi,
                out_dir=args.out_dir,
                anchors=list(args.anchor or []),
                section_anchor=args.section_anchor,
                until_anchor=args.until_anchor,
                all_pages=bool(args.all_pages),
                contact_sheet_requested=bool(args.contact_sheet),
                fresh_session=fresh_session,
            )
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
            else:
                print(manifest['manifest_path'])
                print(f"pdf: {manifest['exported_pdf_path']}")
                print(f"pages requested: {manifest.get('pages_requested_summary') or _format_page_range_summary(manifest.get('pages_requested') or [])}")
                print(f"pages rendered: {manifest.get('pages_rendered_summary') or _format_page_range_summary(manifest.get('pages_rendered') or [])}")
                if manifest.get('current_pdf_page_count') is not None:
                    print(
                        f"current PDF page count: {manifest.get('current_pdf_page_count')} "
                        f"({manifest.get('page_count_source') or 'unknown source'}; metadata only, not validation proof)"
                    )
                for warning in manifest.get('warnings') or []:
                    print(f"warning: {warning}")
                if manifest.get('section_derivation'):
                    print(f"section: {json.dumps(manifest.get('section_derivation'), ensure_ascii=False)}")
                if manifest.get('contact_sheet_path'):
                    print(f"contact sheet: {manifest.get('contact_sheet_path')}")
            return 0

        if args.command == 'style-inspect':
            bundle_args: list[str] = []
            if args.match:
                bundle_args.append(args.match)
            if args.keep_position:
                bundle_args.append('--keep-position')
            _spec, payload = _execute_named_bundle(base_url, 'style-inspect', bundle_args)
            if args.json:
                print(json.dumps(summarize_style_inspect(payload), ensure_ascii=False, indent=2))
            else:
                print(format_style_inspect_human(payload))
            return 0

        if args.command == 'paragraph-style-apply-exact':
            bundle_args = _paragraph_style_apply_bundle_args(args)
            spec, payload = _execute_named_bundle(base_url, 'paragraph-style-apply-exact', bundle_args)
            if not args.json:
                _print_bundle_plan(spec.name, spec.local_metadata())
                print('commit: command executed against the working copy; not saved')
                print('next: run rendered proof before save; use `hwpx save` only after proof passes.')
            _print_bundle_payload(payload, json_output=bool(args.json))
            return 0

        if args.command == 'paragraph-delete-exact':
            bundle_args = _paragraph_delete_bundle_args(args)
            spec, payload = _execute_named_bundle(base_url, 'paragraph-delete-exact', bundle_args)
            if not args.json:
                _print_bundle_plan(spec.name, spec.local_metadata())
                print('commit: exact paragraph/scaffold cleanup executed against the working copy; not saved')
                print('next: run rendered before/after proof and verify official heading hyphens before save.')
            _print_bundle_payload(payload, json_output=bool(args.json))
            return 0

        if args.command == 'text-table-to-native':
            bundle_args = []
            if args.from_file is not None:
                bundle_args.extend(['--from-file', str(args.from_file)])
            if args.text is not None:
                bundle_args.extend(['--text', args.text])
            if args.confirm_native_table:
                bundle_args.append('--confirm-native-table')
            if args.field_name:
                bundle_args.extend(['--field-name', args.field_name])
            if args.split_by_column:
                bundle_args.extend(['--split-by-column', args.split_by_column])
            spec, payload = _execute_named_bundle(base_url, 'text-table-to-native', bundle_args)
            if not args.json:
                _print_bundle_plan(spec.name, spec.local_metadata())
                if args.split_by_column:
                    print('commit: split native table insertion bundle executed against the working copy; old pipe/plain text was not deleted; not saved')
                    print('next: verify every split table with rendered proof before save or any old text cleanup.')
                else:
                    print('commit: native table inserted into the working copy; old pipe/plain text was not deleted; not saved')
                    print('next: run rendered proof before save or any old text cleanup.')
            _print_bundle_payload(payload, json_output=bool(args.json))
            return 0


        if args.command == 'text-table-cleanup-selected':
            bundle_args = []
            if args.from_file is not None:
                bundle_args.extend(['--from-file', str(args.from_file)])
            if args.text is not None:
                bundle_args.extend(['--text', args.text])
            if args.expected_hash:
                bundle_args.extend(['--expected-hash', args.expected_hash])
            if args.native_table_proof_ref:
                bundle_args.extend(['--native-table-proof-ref', args.native_table_proof_ref])
            if args.native_table_proof_hash:
                bundle_args.extend(['--native-table-proof-hash', args.native_table_proof_hash])
            if args.confirm_cleanup:
                bundle_args.append('--confirm-cleanup')
            spec, payload = _execute_named_bundle(base_url, 'text-table-cleanup-selected', bundle_args)
            if not args.json:
                _print_bundle_plan(spec.name, spec.local_metadata())
                print('commit: selected old source text deleted from the working copy only if all exact-selection checks passed; not saved')
                print('next: run rendered before/after proof before save/final delivery.')
            _print_bundle_payload(payload, json_output=bool(args.json))
            return 0

        if args.command == 'table4-anchor-range-replace':
            bundle_args = [
                '--from-file',
                str(args.from_file),
                '--section-anchor',
                args.section_anchor,
                '--start-anchor',
                args.start_anchor,
                '--end-before-anchor',
                args.end_before_anchor,
            ]
            for attr, flag in (
                ('required_source_basename', '--required-source-basename'),
                ('forbid_source_basename', '--forbid-source-basename'),
                ('expected_range_hash', '--expected-range-hash'),
                ('expected_normalized_range_hash', '--expected-normalized-range-hash'),
                ('caption_text', '--caption-text'),
                ('field_name', '--field-name'),
            ):
                value = getattr(args, attr, None)
                if value:
                    bundle_args.extend([flag, str(value)])
            if args.confirm_replace:
                bundle_args.append('--confirm-replace')
            spec, payload = _execute_named_bundle(base_url, 'table4-anchor-range-replace', bundle_args)
            if not args.json:
                _print_bundle_plan(spec.name, spec.local_metadata())
                print('commit: guarded Table 4 source range replaced with a native HWP table in the working copy; not saved')
                print('next: run rendered proof covering Table 4, Figure 5, section continuity, duplicate old source absence, and clipping before save/final delivery.')
            _print_bundle_payload(payload, json_output=bool(args.json))
            return 0

        if args.command in {'style-apply', 'style-clone'}:
            spec = build_named_bundle(args.command, ['--from', args.from_match, '--to', args.to_match, '--dump-spec'])
            print('수정 안 됨 / no mutation performed')
            print(dumps_debug_payload(spec))
            return 0

        if args.command == 'qa-profile':
            bundle_args: list[str] = ['--out-dir', str(args.out_dir)]
            if args.section_anchor:
                bundle_args.extend(['--section', args.section_anchor])
            if args.until_anchor:
                bundle_args.extend(['--until-anchor', args.until_anchor])
            for token in args.forbid or []:
                bundle_args.extend(['--forbid', token])
            for token in args.require or []:
                bundle_args.extend(['--require', token])
            for pair in args.after_anchor_forbid or []:
                bundle_args.extend(['--after-anchor-forbid', pair])
            if args.source_hash:
                bundle_args.extend(['--source-hash', args.source_hash])
            _spec, payload = _execute_named_bundle(base_url, 'qa-profile', bundle_args)
            manifest = _run_qa_profile(
                base_url=base_url,
                raw_payload=payload,
                out_dir=args.out_dir,
                section_anchor=args.section_anchor,
                until_anchor=args.until_anchor,
                forbid=list(args.forbid or []),
                require=list(args.require or []),
                after_anchor_forbid=list(args.after_anchor_forbid or []),
                source_hash=args.source_hash,
            )
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
            else:
                print(f"qa: {'PASS' if manifest.get('ok') else 'FAIL'}")
                print(f"manifest: {manifest.get('manifest_path')}")
                print(f"pdf: {manifest.get('exported_pdf_path')}")
                for failure in manifest.get('failures') or []:
                    print(f"failure: {failure}")
            return 0

        if args.command == 'save':
            _state_before_save, save_expected_generation, save_expected_session = _state_cas_snapshot()
            payload = post_json(base_url, '/local-cli/save', {'session_id': save_expected_session})
            destination = _download_artifact(payload, kind='working-copy', base_url=base_url, out=getattr(args, 'out', None))
            update_state(lambda state: {
                **state,
                'last_saved_working_copy_path': str(destination),
                'last_saved_working_copy_sha256': f'sha256:{_sha256_file(destination)}',
            }, expected_generation=save_expected_generation, expected_session_id=save_expected_session)
            _print_artifact_result(
                role='saved working copy',
                path=destination,
                next_step='export/render proof (`hwpx export-proof-range` or `hwpx page-screenshot`) and review before final delivery.',
            )
            return 0

        if args.command == 'working-copy':
            _state_before_working_copy, working_copy_expected_generation, working_copy_expected_session = _state_cas_snapshot()
            session_id = working_copy_expected_session
            if not session_id:
                raise ApiError('No active local CLI session is open.')
            destination = download_to_path(
                base_url,
                f'/local-cli/session/{session_id}/artifact/working-copy',
                _artifact_destination('working-copy'),
            )
            update_state(lambda state: {
                **state,
                'last_saved_working_copy_path': str(destination),
                'last_saved_working_copy_sha256': f'sha256:{_sha256_file(destination)}',
            }, expected_generation=working_copy_expected_generation, expected_session_id=working_copy_expected_session)
            _print_artifact_result(
                role='downloaded saved working copy',
                path=destination,
                next_step='review the latest rendered proof or run `hwpx export-proof-range` before reporting completion.',
            )
            return 0

        if args.command == 'undo':
            post_json(base_url, '/local-cli/undo', {'session_id': _state_session_id()})
            print('ok')
            return 0

        if args.command == 'redo':
            post_json(base_url, '/local-cli/redo', {'session_id': _state_session_id()})
            print('ok')
            return 0

        if args.command == 'table':
            payload = post_json(
                base_url,
                '/local-cli/table',
                {'cols': args.cols, 'rows': args.rows, 'session_id': _state_session_id()},
            )
            print(f"ok: table {payload.get('cols')}x{payload.get('rows')}")
            _print_current_state(payload)
            return 0

        if args.command == 'list':
            payload = post_json(
                base_url,
                '/local-cli/list',
                {'count': args.count, 'session_id': _state_session_id()},
            )
            print(f"ok: list {payload.get('count')}")
            _print_current_state(payload)
            return 0

        if args.command == 'screenshot':
            if args.mode == 'page':
                destination = _page_screenshot(base_url=base_url, page=args.page, dpi=args.dpi, out=getattr(args, 'out', None), out_dir=getattr(args, 'out_dir', None))
                manifest_raw = load_state().get('last_page_screenshot_manifest_path')
                manifest_path = Path(str(manifest_raw)) if manifest_raw else None
                _print_artifact_result(
                    role='rendered page proof',
                    path=destination,
                    manifest_path=manifest_path,
                    next_step='review visible target text/layout/no clipping; then save/report if proof passes.',
                    json_output=bool(args.json),
                    command='page-screenshot',
                )
                return 0
            _state_before_screenshot, screenshot_expected_generation, screenshot_expected_session = _state_cas_snapshot()
            payload = post_json(base_url, '/local-cli/screenshot', {'session_id': screenshot_expected_session})
            destination = _download_artifact(payload, kind='screenshot', base_url=base_url)
            update_state(
                lambda state: {**state, 'last_screenshot_path': str(destination)},
                expected_generation=screenshot_expected_generation,
                expected_session_id=screenshot_expected_session,
            )
            _print_artifact_result(
                role='live editor proof',
                path=destination,
                next_step='confirm live Hancom frame/caret; for layout proof run `hwpx screenshot --mode page` or `hwpx export-proof-range`.',
                json_output=bool(args.json),
            )
            return 0

        if args.command == 'page-screenshot':
            destination = _page_screenshot(base_url=base_url, page=args.page, dpi=args.dpi, out=getattr(args, 'out', None), out_dir=getattr(args, 'out_dir', None))
            manifest_raw = load_state().get('last_page_screenshot_manifest_path')
            manifest_path = Path(str(manifest_raw)) if manifest_raw else None
            _print_artifact_result(
                role='rendered page proof',
                path=destination,
                manifest_path=manifest_path,
                next_step='review visible target text/layout/no clipping; then save/report if proof passes.',
                json_output=bool(args.json),
                command='page-screenshot',
            )
            return 0

        if args.command == 'export':
            if args.bundle_proof:
                destination, manifest_path = _export_pdf_via_bundle(base_url)
            else:
                _state_before_export, export_expected_generation, export_expected_session = _state_cas_snapshot()
                payload = post_json(base_url, '/local-cli/export', {'session_id': export_expected_session})
                destination = _download_artifact(payload, kind='export', base_url=base_url, out=getattr(args, 'out', None))
                manifest_path = _write_artifact_manifest(
                    'export',
                    destination,
                    extra={
                        'server_artifact_path': payload.get('artifact_path'),
                        'download_path': payload.get('download_path'),
                        'route': '/local-cli/export',
                    },
                )
                update_state(
                    lambda state: {
                        **state,
                        'last_export_path': str(destination),
                        'last_export_manifest_path': str(manifest_path),
                        'last_export_route': 'direct:/local-cli/export',
                    },
                    expected_generation=export_expected_generation,
                    expected_session_id=export_expected_session,
                )
            _print_artifact_result(
                role='exported PDF proof source',
                path=destination,
                manifest_path=manifest_path,
                next_step='render/review pages with `hwpx page-screenshot` or prefer `hwpx export-proof-range` for manifest-backed proof.',
                json_output=bool(args.json),
            )
            return 0

        if args.command == 'close':
            state = load_state()
            close_expected_generation = int(state.get('state_generation', 0))
            session_id = _state_session_id()
            active_document = state.get('source_filename') or 'active live document'
            candidate_identity = _read_candidate_identity()
            close_payload = post_json(base_url, '/local-cli/close', {'session_id': session_id})
            status_after = get_json(base_url, '/local-cli/status')
            clear_session_binding(
                expected_generation=close_expected_generation,
                expected_session_id=session_id,
            )
            if args.json:
                print(json.dumps({
                    'schema_version': 'local-cli/lifecycle/v1',
                    'ok': bool(close_payload.get('ok', True)),
                    'command': 'close',
                    'base_url': base_url,
                    'source_path': state.get('source_path'),
                    'source_filename': active_document,
                    'session_id': session_id,
                    'working_copy_id': close_payload.get('working_copy_id') or session_id,
                    'live_session_bound': bool(status_after.get('live_session_bound')),
                    'close_confirmed': not bool(status_after.get('live_session_bound')),
                    **candidate_identity,
                    'response': close_payload,
                    'status_after': status_after,
                }, ensure_ascii=False, indent=2))
                return 0
            _print_lifecycle_result(
                where=f'{active_document}; session={session_id or "unknown"}',
                how='direct /local-cli/close lifecycle route',
                changed='live session binding closed/cleared; original source file untouched',
                proof='none from close; use prior rendered proof/export manifest for delivery evidence',
                next_step='open another file or archive/report the saved working copy plus rendered proof.',
            )
            return 0

        if args.command == 'type':
            payload = post_json(
                base_url,
                '/local-cli/type',
                {
                    'text': args.text,
                    'session_id': _state_session_id(),
                    'allow_insert_at_caret': bool(args.insert_at_caret),
                },
            )
            _print_command_payload(payload)
            return 0

        if args.command in {'insert-before-anchor', 'insert-after-anchor', 'insert-after-paragraph', 'insert-before-heading'}:
            body = _read_optional_text_arg(inline=args.text, file_path=args.from_file, label='text')
            if not body:
                raise ApiError(f'{args.command} requires non-empty text.')
            payload = post_json(
                base_url,
                '/local-cli/anchor-insert',
                {
                    'target': args.target,
                    'text': body,
                    'position': args.anchor_insert_position,
                    'session_id': _state_session_id(),
                },
            )
            _print_command_payload(payload)
            return 0

        if args.command == 'figure-section':
            intro = _read_optional_text_arg(inline=args.intro, file_path=args.intro_file, label='intro')
            caption = _read_optional_text_arg(inline=args.caption, file_path=args.caption_file, label='caption')
            body = _read_optional_text_arg(inline=args.body, file_path=args.body_file, label='body')
            request_payload: dict[str, Any] = {
                'target_heading': args.target_heading,
                'heading': args.heading,
                'intro': intro,
                'caption': caption,
                'body': body,
                'session_id': _state_session_id(),
            }
            if args.image is not None:
                allowed_suffixes = {'.png', '.jpg', '.jpeg', '.bmp'}
                if not args.image.exists() or not args.image.is_file():
                    raise ApiError(f'Local image file not found: {args.image}')
                if args.image.stat().st_size <= 0:
                    raise ApiError(f'Local image file is empty: {args.image}')
                suffix = args.image.suffix.lower()
                if suffix not in allowed_suffixes:
                    raise ApiError(f'Unsupported image type: {suffix or "<none>"}. Allowed: {", ".join(sorted(allowed_suffixes))}')
                extra_fields = {key: '' if value is None else str(value) for key, value in request_payload.items()}
                if args.width is not None:
                    extra_fields['width'] = str(args.width)
                if args.height is not None:
                    extra_fields['height'] = str(args.height)
                if args.sizeoption is not None:
                    extra_fields['sizeoption'] = str(args.sizeoption)
                if args.treat_as_char is not None:
                    extra_fields['treat_as_char'] = args.treat_as_char
                if args.embedded is not None:
                    extra_fields['embedded'] = args.embedded
                if args.fit_cell:
                    extra_fields['fit_cell'] = 'true'
                payload = post_file(base_url, '/local-cli/figure-section-image', field_name='image', file_path=args.image, extra_fields=extra_fields)
            else:
                payload = post_json(base_url, '/local-cli/figure-section', request_payload)
            _print_command_payload(payload)
            return 0

        if args.command == 'image':
            allowed_suffixes = {'.png', '.jpg', '.jpeg', '.bmp'}
            if not args.file.exists() or not args.file.is_file():
                raise ApiError(f'Local image file not found: {args.file}')
            if args.file.stat().st_size <= 0:
                raise ApiError(f'Local image file is empty: {args.file}')
            suffix = args.file.suffix.lower()
            if suffix not in allowed_suffixes:
                raise ApiError(f'Unsupported image type: {suffix or "<none>"}. Allowed: {", ".join(sorted(allowed_suffixes))}')
            extra_fields: dict[str, str] = {'session_id': _state_session_id() or ''}
            if args.width is not None:
                extra_fields['width'] = str(args.width)
            if args.height is not None:
                extra_fields['height'] = str(args.height)
            if args.sizeoption is not None:
                extra_fields['sizeoption'] = str(args.sizeoption)
            if args.treat_as_char is not None:
                extra_fields['treat_as_char'] = args.treat_as_char
            if args.embedded is not None:
                extra_fields['embedded'] = args.embedded
            if args.fit_cell:
                extra_fields['fit_cell'] = 'true'
            payload = post_file(base_url, '/local-cli/image', field_name='file', file_path=args.file, extra_fields=extra_fields)
            _print_image_payload(payload)
            return 0

        if args.command == 'image-at-anchor':
            allowed_suffixes = {'.png', '.jpg', '.jpeg', '.bmp'}
            if not args.file.exists() or not args.file.is_file():
                raise ApiError(f'Local image file not found: {args.file}')
            if args.file.stat().st_size <= 0:
                raise ApiError(f'Local image file is empty: {args.file}')
            suffix = args.file.suffix.lower()
            if suffix not in allowed_suffixes:
                raise ApiError(f'Unsupported image type: {suffix or "<none>"}. Allowed: {", ".join(sorted(allowed_suffixes))}')
            extra_fields: dict[str, str] = {
                'session_id': _state_session_id() or '',
                'target': args.target,
                'position': args.position,
            }
            if args.width is not None:
                extra_fields['width'] = str(args.width)
            if args.height is not None:
                extra_fields['height'] = str(args.height)
            if args.sizeoption is not None:
                extra_fields['sizeoption'] = str(args.sizeoption)
            if args.treat_as_char is not None:
                extra_fields['treat_as_char'] = args.treat_as_char
            if args.embedded is not None:
                extra_fields['embedded'] = args.embedded
            if args.fit_cell:
                extra_fields['fit_cell'] = 'true'
            payload = post_file(base_url, '/local-cli/image-at-anchor', field_name='file', file_path=args.file, extra_fields=extra_fields)
            _print_image_payload(payload)
            warnings = payload.get('warnings') if isinstance(payload.get('warnings'), list) else []
            for warning in warnings:
                print(f'warning: {warning}')
            if payload.get('proof_required'):
                print(f"proof: {payload.get('proof_required')}")
            return 0

        if args.command == 'replace':
            raise ApiError(f'replace is disabled: {NO_MUTATION_TEXT}; use find/select/proof or a future bundle-backed atomic replace primitive.')

        if args.command in {'cell-replace', 'cell-replace-exact'}:
            if args.text is not None and args.text_file is not None:
                raise ApiError('Use either --text or --text-file, not both.')
            request: dict[str, Any] = {'session_id': _state_session_id()}
            if args.anchor:
                request['anchor'] = args.anchor
            if args.cell:
                request['cell'] = args.cell
            if args.expected_page is not None:
                request['expected_page'] = args.expected_page
            if args.expect_cell:
                request['expect_cell'] = args.expect_cell
            if args.expect_old:
                request['expect_old'] = args.expect_old
            if args.expect_new:
                request['expect_new'] = args.expect_new
            if args.text_file is not None:
                if not args.text_file.exists() or not args.text_file.is_file():
                    raise ApiError(f'Local text file not found: {args.text_file}')
                body = args.text_file.read_text(encoding='utf-8')
                if not body:
                    raise ApiError(f'Local text file is empty: {args.text_file}')
                request['text'] = body
                request['text_file'] = str(args.text_file.resolve())
            elif args.text is not None:
                request['text'] = args.text
            else:
                raise ApiError('cell-replace requires --text or --text-file.')
            payload = post_json(base_url, '/local-cli/cell-replace', request)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_cell_replace_payload(payload)
            return 0

        if args.command == 'fontsize':
            payload = post_json(
                base_url,
                '/local-cli/fontsize',
                {'size_pt': args.size_pt, 'session_id': _state_session_id()},
            )
            _print_command_payload(payload)
            return 0

        if args.command == 'bold':
            payload = post_json(
                base_url,
                '/local-cli/bold',
                {'enabled': args.value == 'on', 'session_id': _state_session_id()},
            )
            _print_command_payload(payload)
            return 0

        if args.command == 'font':
            payload = post_json(
                base_url,
                '/local-cli/font',
                {'face_name': args.face_name, 'session_id': _state_session_id()},
            )
            _print_command_payload(payload)
            return 0

        if args.command == 'bullet':
            payload = post_json(base_url, '/local-cli/bullet', {'text': args.text, 'session_id': _state_session_id()})
            _print_command_payload(payload)
            return 0

        if args.command == 'pycall':
            call_args = _parse_json_cli_value(args.args_json, expected_type=list, label='args_json')
            call_kwargs = _parse_json_cli_value(args.kwargs_json, expected_type=dict, label='kwargs_json')
            payload = post_json(
                base_url,
                '/local-cli/pycall',
                {
                    'method_path': args.method_path,
                    'args': call_args,
                    'kwargs': call_kwargs,
                    'session_id': _state_session_id(),
                },
            )
            _print_macro_payload(payload)
            return 0

        if args.command == 'action':
            payload = post_json(
                base_url,
                '/local-cli/action',
                {'action_name': args.action_name, 'session_id': _state_session_id()},
            )
            _print_macro_payload(payload)
            return 0

        if args.command in {'bundle-list', 'bundles'}:
            _print_bundle_list()
            return 0

        if args.command == 'bundle-help':
            print(bundle_help(args.bundle_name))
            return 0

        if args.command == 'tx-preview':
            _write_tx_preview(args.out_json, raw_args=list(args.tx_args or []))
            return 0

        if args.command == 'tx-commit':
            request_payload = _load_tx_server_payload(args.plan_json)
            payload = post_json(base_url, '/local-cli/command-bundle', request_payload)
            if not args.json:
                print('transaction committed: server_payload from preview was posted unchanged')
                print('commit: working copy may be dirty; not saved')
                print('next: run rendered proof before save; use `hwpx save` only after proof passes.')
            _print_bundle_payload(payload, json_output=bool(args.json))
            return 0

        if args.command == 'bundle-dump':
            spec = build_named_bundle(args.bundle_name, args.bundle_args)
            if args.with_meta:
                print(dumps_debug_payload(spec))
            else:
                print(dumps_server_payload(spec))
            return 0

        if args.command in {'create-bundle', 'bundle-create', 'bundle-compose'}:
            _write_created_bundle(
                args.out_json,
                recipes=args.recipes,
                steps=args.steps,
                with_meta=args.with_meta,
                force=args.force,
            )
            return 0

        if args.command == 'bundle-run':
            spec, payload = _execute_named_bundle(base_url, args.bundle_name, args.bundle_args)
            if not args.json:
                _print_bundle_plan(spec.name, spec.local_metadata())
            _print_bundle_payload(payload, json_output=args.json)
            return 0

        if args.command == 'bundle':
            request_payload = _load_bundle_payload(args.json_file)
            payload = post_json(base_url, '/local-cli/command-bundle', request_payload)
            _print_bundle_payload(payload, json_output=args.json)
            return 0
    except (ApiError, BundleError, StatePersistenceError) as exc:
        message = exc.message if isinstance(exc, ApiError) else str(exc)
        print(f'error: {message}', file=sys.stderr)
        print('next: run `hwpx status` to confirm runtime/session, or `hwpx help workflow` for the safe edit+proof loop.', file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
