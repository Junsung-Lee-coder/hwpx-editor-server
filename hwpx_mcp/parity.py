"""Backend-to-MCP parity contract.

Every backend command-bundle op, REST route and queued edit operation is
classified here:

- exposed: reachable through the named MCP tool with the backend's own fields.
- equivalent: the same backend capability is reached through another MCP tool
  that keeps its options, scope and result (named in `via`).
- excluded: deliberately not reachable, with a concrete reason.
- backend_unsupported: the backend route exists but refuses to work.

hwpx_capabilities returns this contract, and tests/test_mcp_backend_parity.py
fails whenever the backend gains an op, route, field or queued-operation read
that is not classified here. Pure data: no backend import.
"""
from __future__ import annotations

from typing import Any

SESSION_TOOLS = 'hwpx_command/hwpx_bundle'


def _exposed(mcp: str, **extra: Any) -> dict[str, Any]:
    return {'status': 'exposed', 'mcp': mcp, **extra}


def _equivalent(via: str, reason: str) -> dict[str, Any]:
    return {'status': 'equivalent', 'mcp': via, 'reason': reason}


def _excluded(reason: str) -> dict[str, Any]:
    return {'status': 'excluded', 'reason': reason}


_LEGACY = 'legacy native control-inventory path (target_id ctrl/..), not the exact table_id+hash engine; any backend error holds the session'
_UNDO_REFUSED = 'backend Undo policy refuses this step (local_cli_service._BUNDLE_UNDO_POLICY)'
_UNGUARDED = ('not a guarded exact engine: any backend error in the step, including a stale or wrong target, is an unknown outcome '
              'that holds the session (local_cli_service command_bundle; measured on Hancom 2024 with get_selected_text without a '
              'selection and table_cell_structure_exact on a section control)')

# --------------------------------------------------------------------------- command-bundle ops
BUNDLE_OPS: dict[str, dict[str, Any]] = {
    'context': _exposed(SESSION_TOOLS),
    'selection_proof': _exposed(SESSION_TOOLS),
    'readback': _exposed(SESSION_TOOLS, note='scope caret|selection|page|document; MCP default document'),
    'where': _exposed(SESSION_TOOLS, note='compact location; hwpx_where gives the full standalone observation'),
    'cell_format_exact': _exposed(SESSION_TOOLS, note=f'{_LEGACY}; fill_color/border=none added for parity'),
    'page_number_apply': _exposed(SESSION_TOOLS),
    'official_numbering_apply': _exposed(SESSION_TOOLS),
    'paragraph_insert_exact': _exposed(SESSION_TOOLS),
    'paragraph_remove_exact': _exposed(SESSION_TOOLS),
    'character_format_exact': _exposed(SESSION_TOOLS),
    'paragraph_format_exact': _exposed(SESSION_TOOLS),
    'table_inventory': _exposed(SESSION_TOOLS),
    'table_read': _exposed(SESSION_TOOLS),
    'table_edit_exact': _exposed(SESSION_TOOLS),
    'table_style_exact': _exposed(SESSION_TOOLS),
    'table_width_exact': _exposed(SESSION_TOOLS),
    'table_create_exact': _exposed(SESSION_TOOLS),
    'control_inventory': _exposed(SESSION_TOOLS, note='read-only native control list with target_id/expected_hash'),
    'table_frame_inventory': _exposed(SESSION_TOOLS, note='read-only table frame list'),
    'exact_control_select_proof': _exposed(SESSION_TOOLS, note='read-only selection proof of one control'),
    'table_cell_structure_exact': _exposed(SESSION_TOOLS, note='read-only cell structure of one table control'),
    'control_delete_exact': _exposed(SESSION_TOOLS, note=_LEGACY),
    'control_move_resize_exact': _exposed(SESSION_TOOLS, note=f'{_LEGACY}; scale 5..200%, move within 300 mm'),
    'control_join_previous_exact': _exposed(SESSION_TOOLS, note=_LEGACY),
    'cell_row_fit_exact': _exposed(SESSION_TOOLS, note=_LEGACY),
    'table_split_exact': _exposed(SESSION_TOOLS, note=_LEGACY),
    'table_structure_exact': _exposed(SESSION_TOOLS, note=f'{_LEGACY}; table_edit_exact is the exact table_id engine'),
    'table_column_width_exact': _equivalent('hwpx_command:table_width_exact', 'the legacy engine refuses any request without four '
                                            'whole-document SHA-256 guards (working-copy preimage, cell inventory, document text, BinData manifest) '
                                            'and positive counts, and no public read returns the inventory or BinData hashes; table_width_exact '
                                            'sets the same thing (the width in mm of every column of one table, from table_inventory table_id+hash '
                                            'instead of control_inventory target_id+hash; requested_widths_mm maps to column_widths_mm) and '
                                            'verifies the widths after the edit. It does not require the total to stay the same, and it refuses '
                                            'before any edit (table_would_overflow) widths that would make the table wider than the text, where '
                                            'the legacy engine only keeps the current total; fit=text_width brings a wider table inside first'),
    'native_table_insert': _exposed(SESSION_TOOLS, note='inserts a native table at the caret with field names'),
    'anchor_range_replace_native_table': _exposed(SESSION_TOOLS, note='replaces a proven text range with a 4-column native table'),
    'selected_text_delete_exact': _exposed(SESSION_TOOLS, note='deletes the exactly selected source text after a table replacement'),
    'paragraph_style_apply_exact': _exposed(SESSION_TOOLS, note='keep_with_next, widow_orphan, pagebreak_before'),
    'paragraph_delete_exact': _exposed(SESSION_TOOLS),
    'paragraph_join_previous_exact': _exposed(SESSION_TOOLS),
    'paragraph_join_next_exact': _exposed(SESSION_TOOLS),
    'paragraph_rehome_exact': _exposed(SESSION_TOOLS),
    'anchor_insert': _exposed(SESSION_TOOLS, note='before-anchor, after-anchor, after-paragraph, before-heading; text or fragments'),
    'set_text_file': _exposed(SESSION_TOOLS, note='UNICODE insertfile at the caret only'),
    'get_selected_text': _exposed(SESSION_TOOLS),
    'style_inspect': _exposed(SESSION_TOOLS),
    'typography_overview': _exposed(SESSION_TOOLS),
    'layout_exact': _exposed(SESSION_TOOLS, note=f'page_setup, columns, section_insert (only between two characters of one paragraph\'s text), '
                                                f'section_delete, hanging_indent; native qualification per variant in variants; {_UNDO_REFUSED}'),
    'layout_inspect': _exposed(SESSION_TOOLS, note='read-only page and column definition at the caret'),
    'object_insert_exact': _exposed(SESSION_TOOLS, note=f'footnote, endnote, memo, hyperlink, bookmark, equation, line, rectangle, ellipse, header, footer; '
                                                       f'native qualification per variant in variants; {_UNDO_REFUSED}'),
    'hwp_action': _exposed(SESSION_TOOLS, note='only the backend command-bundle navigation/selection allowlist'),
    'pyhwpx_call': _exposed(SESSION_TOOLS, note='only the backend command-bundle read/caret allowlist'),
    'export_pdf': _equivalent('hwpx_proof', 'hwpx_proof page/review runs the same backend export, then downloads, renders and checks the PDF; a bare bundle export leaves the PDF unretrieved'),
    'save_document': _equivalent('hwpx_save', 'hwpx_save runs the backend save and copies, verifies and records the saved file that hwpx_close relies on; a bare bundle save bypasses that retrieval'),
}

# Backend ops with a semantic pre-edit refusal path (local_cli_service._GUARDED_BUNDLE_OPS); every other
# exposed op reports a backend error as an unknown outcome that holds the session.
GUARDED = frozenset({'page_number_apply', 'official_numbering_apply', 'paragraph_insert_exact', 'paragraph_remove_exact',
                     'character_format_exact', 'paragraph_format_exact', 'table_inventory', 'table_read', 'table_edit_exact',
                     'table_style_exact', 'table_width_exact', 'table_create_exact'})
# Ops that publish mutation intent at their first native edit (app.step_refusal.DEFERRED_INTENT_OPS): before it they
# refuse with a fixed refusal_code only from their own exact reads; anything else holds the session.
DEFERRED = frozenset({'layout_exact', 'object_insert_exact', 'paragraph_rehome_exact'})
_DEFERRED = ('refused with a fixed refusal_code, document unchanged, caret put back (an existing selection may be cleared) and session usable, only when the backend proves the '
             'request stale or impossible from its own exact reads before the first native edit; any other backend error, and any '
             'failure after the edit started, is an unknown outcome that holds the session (app.step_refusal)')
for _op, _row in BUNDLE_OPS.items():
    if _row['status'] == 'exposed' and _op not in GUARDED:
        _row['error_hold'] = _DEFERRED if _op in DEFERRED else _UNGUARDED

# --------------------------------------------------------------------------- native qualification per variant
# Reference qualification on Hancom Office 2024 for Windows: what the saved-output evidence for this source covers. It
# does not say that another host, Hancom version or document behaves the same.
NATIVE_REFERENCE = ('Reference native qualification on Hancom Office 2024 (Windows) for this source: qualified = a saved, reopened and '
                    'rendered output of the variant was checked from the saved bytes against the opened document; qualified_unaffected = '
                    'that evidence comes from an earlier source and nothing on the variant\'s code path changed since; not_qualified = '
                    'the backend runs the variant but no such evidence exists; unsupported = refused before any edit with the named '
                    'refusal_code. It does not cover other hosts, Hancom versions or documents.')
NATIVE_STATUSES = ('qualified', 'qualified_unaffected', 'not_qualified', 'unsupported')


def _variant(kind: str, qualifier: str | None, scope: str, native: str, **extra: str) -> dict[str, str]:
    assert native in NATIVE_STATUSES
    return {'variant': kind if qualifier is None else f'{kind}:{qualifier}', 'kind': kind, 'scope': scope, 'native': native, **extra}


VARIANTS: dict[str, list[dict[str, str]]] = {
    'layout_exact': [
        _variant('page_setup', 'margins', 'margin fields, apply_to current_section', 'qualified'),
        _variant('page_setup', 'landscape', 'landscape true, apply_to current_section', 'qualified'),
        _variant('page_setup', 'paper_size', 'paper_width_mm/paper_height_mm', 'not_qualified'),
        _variant('page_setup', 'whole_document', 'apply_to whole_document', 'not_qualified'),
        _variant('columns', 'current_section', 'count/gap_mm/same_width, apply_to current_section', 'qualified'),
        _variant('columns', 'from_caret_new', 'apply_to from_caret_new (new columns from the caret)', 'not_qualified'),
        _variant('section_insert', 'inside_paragraph', 'caret between two characters of one paragraph\'s text; the two-section '
                 'document saved, reopened and rendered, outline numbering after the break kept', 'qualified'),
        _variant('section_insert', 'paragraph_start_or_end', 'caret at a paragraph\'s first text (also after its leading section '
                 'and column definitions), at its end or in a paragraph without text', 'unsupported',
                 refusal_code='section_insert_position_unsupported'),
        _variant('section_delete', None, 'caret at the first text of section_index in a saved multi-section document opened on its own',
                 'qualified'),
        _variant('hanging_indent', None, 'marker_text at the paragraph start', 'qualified'),
    ],
    'object_insert_exact': [
        _variant('footnote', None, 'text at the caret', 'qualified'),
        _variant('endnote', None, 'text at the caret', 'qualified'),
        _variant('memo', None, 'text at the caret, no selection', 'qualified'),
        _variant('memo', 'selection', 'text on a live selection', 'not_qualified'),
        _variant('hyperlink', None, 'url on the live selection of display_text', 'qualified'),
        _variant('bookmark', None, 'new name at the caret', 'qualified'),
        _variant('equation', None, 'script at the caret', 'qualified'),
        _variant('line', 'inline', 'treat_as_char true', 'qualified'),
        _variant('line', 'floating', 'treat_as_char false or unset', 'not_qualified'),
        _variant('rectangle', 'inline', 'treat_as_char true', 'qualified'),
        _variant('rectangle', 'floating', 'treat_as_char false or unset', 'not_qualified'),
        _variant('ellipse', 'inline', 'treat_as_char true', 'qualified'),
        _variant('ellipse', 'floating', 'treat_as_char false or unset', 'not_qualified'),
        _variant('header', 'both', 'apply_to both', 'qualified'),
        _variant('header', 'odd', 'apply_to odd', 'qualified'),
        _variant('header', 'even', 'apply_to even', 'not_qualified'),
        _variant('footer', 'both', 'apply_to both', 'qualified'),
        _variant('footer', 'odd', 'apply_to odd', 'not_qualified'),
        _variant('footer', 'even', 'apply_to even', 'qualified'),
    ],
}
for _op, _variants in VARIANTS.items():
    BUNDLE_OPS[_op]['variants'] = _variants

EXCLUDED_FIELDS: dict[str, dict[str, str]] = {}

# --------------------------------------------------------------------------- REST routes
_SID = {'session_id': 'session_id'}


def _same(*names: str) -> dict[str, str]:
    return {name: name for name in names}


def _cmd(op: str, *names: str) -> dict[str, Any]:
    return _exposed(f'hwpx_command:{op}', fields={**_SID, **_same(*names)})


# Navigation-only live commands (local_cli_runtime _EXACT_CLEAN_EXEMPT_COMMANDS): with no earlier debt and IsModified
# exactly False before and after they add no close debt; earlier unsaved edits still need a writing save.
_CARET_DEBT = 'navigation only: adds no close debt when the document is unchanged and had no unsaved edit; an earlier unsaved edit still needs a writing save'


_LEDGER = ('operator ledger write: records caller-reported find/choose/lock/verify/apply/undo/close results without editing the document; '
           'from MCP it could overwrite the managed session lifecycle record (for example a close outcome) that hwpx_* admission relies on')
_VIEWER = 'live screen viewer UI/stream for a human operator, not a document operation; hwpx_proof frame returns the native frame'
_EVIDENCE = 'evidence frames of operator ledger verify steps; MCP-managed sessions never record them'
_FROM_PATH = ('equivalent upload variant: hwpx_job sends the confined local file to the upload route, which runs the same compile/enqueue '
              'code; an arbitrary backend filesystem path is not exposed')
_IMAGE_FIELDS = {'session_id': 'session_id', 'width': 'width', 'height': 'height', 'sizeoption': 'sizeoption',
                 'treat_as_char': 'treat_as_char', 'embedded': 'embedded', 'fit_cell': 'fit_cell'}

REST_ROUTES: dict[str, dict[str, Any]] = {
    'GET /local-cli/status': _exposed('hwpx_status', fields={}),
    'POST /local-cli/command-reconcile': _exposed('hwpx_command:command_reconcile', fields={**_SID, 'command_id': 'command_id'}),
    'POST /local-cli/open': _exposed('hwpx_open', fields={'file': 'source_path', 'session_label': 'session_label'}),
    'POST /local-cli/find': _exposed('hwpx_find', fields={**_SID, **_same('query', 'around', 'with_page', 'proof_match')}),
    'POST /local-cli/info': _cmd('target_info', 'target'),
    'POST /local-cli/move': _cmd('move_to_target', 'target') | {'close_note': _CARET_DEBT},
    'POST /local-cli/select': _cmd('select_target', 'target') | {'close_note': _CARET_DEBT},
    'POST /local-cli/cell': _cmd('cell_info') | {'close_note': _CARET_DEBT},
    'POST /local-cli/cellmove': _cmd('cell_move', 'direction', 'count') | {'close_note': _CARET_DEBT},
    'POST /local-cli/cursormove': _cmd('cursor_move', 'direction', 'count') | {'close_note': _CARET_DEBT},
    'POST /local-cli/type': _cmd('type_text', 'text', 'allow_insert_at_caret'),
    'POST /local-cli/anchor-insert': _equivalent('hwpx_command:anchor_insert', 'same _perform_anchor_insert primitive and fields (target, text, position) through the command bundle, which also accepts fragments'),
    'POST /local-cli/figure-section': _cmd('figure_section', 'target_heading', 'heading', 'intro', 'caption', 'body'),
    'POST /local-cli/figure-section-image': _exposed('hwpx_image:figure_section', fields={
        **_IMAGE_FIELDS, 'image': 'source_path', **_same('target_heading', 'heading', 'intro', 'caption', 'body')}),
    'POST /local-cli/image': _exposed('hwpx_image:insert', fields={**_IMAGE_FIELDS, 'file': 'source_path'}),
    'POST /local-cli/image-at-anchor': _exposed('hwpx_image:insert_at_anchor', fields={**_IMAGE_FIELDS, 'file': 'source_path', 'target': 'target', 'position': 'position'}),
    'POST /local-cli/replace': {'status': 'backend_unsupported', 'reason': 'the backend route answers 501: atomic replace is disabled because delete+insert could corrupt page flow'},
    'POST /local-cli/cell-replace': {**_cmd('cell_replace', 'anchor', 'cell', 'text', 'expect_cell', 'expect_old', 'expect_new', 'expected_page'),
                                     'excluded_fields': {'text_file': 'reads an arbitrary path on the backend machine; send the content as text'}},
    'POST /local-cli/fontsize': _cmd('font_size', 'size_pt', 'at_caret'),
    'POST /local-cli/bold': _cmd('bold', 'enabled'),
    'POST /local-cli/font': _cmd('font_family', 'face_name', 'at_caret'),
    'POST /local-cli/bullet': {**_cmd('bullet', 'text'), 'excluded_fields': {
        'allow_insert_at_caret': 'present only because the route shares LocalCliTextRequest; the bullet handler never reads it'}},
    'POST /local-cli/numbering-readback': _cmd('numbering_readback'),
    'POST /local-cli/undo': _exposed('hwpx_command:undo', fields=_SID),
    'POST /local-cli/redo': _exposed('hwpx_command:redo', fields=_SID),
    'POST /local-cli/close': _exposed('hwpx_close', fields=_SID),
    'POST /local-cli/table': _cmd('table_insert', 'rows', 'cols'),
    'POST /local-cli/list': {**_cmd('list_items', 'count'), 'backend_unsupported_variants': {
        'count>1': 'the items are typed as one multi-line text and the backend disables multi-line typing; the service refuses it with 400 before the runtime'}},
    'POST /local-cli/pycall': _excluded('unbounded public pyhwpx method path and arguments (any attribute chain); the bounded '
                                        'command-bundle allowlist is exposed as hwpx_command pyhwpx_call'),
    'POST /local-cli/action': _excluded('unbounded HAction name (destructive actions included); the bounded command-bundle '
                                        'allowlist is exposed as hwpx_command hwp_action'),
    'POST /local-cli/command-bundle': _exposed('hwpx_command/hwpx_bundle', fields={**_SID, 'steps': 'request.steps'}),
    'POST /local-cli/screenshot': _exposed('hwpx_proof:frame', fields=_SID),
    'POST /local-cli/save': _exposed('hwpx_save', fields=_SID),
    'POST /local-cli/export': _exposed('hwpx_proof:page/review', fields=_SID),
    'POST /local-cli/where': _exposed('hwpx_where', fields=_SID),
    'POST /local-cli/cell-margins-get': _exposed('hwpx_cell_margins_get', fields={**_SID, 'request': 'request'}),
    'GET /local-cli/session/{session_id}/artifact/{kind}': _exposed(
        'hwpx_save/hwpx_close (working-copy), hwpx_proof (export, screenshot), hwpx_artifact (screenshot, recovery)', fields={}),
    'GET /health': _exposed('hwpx_health', fields={}),
    'GET /runtime-readiness': _exposed('hwpx_capabilities', fields={}),
    'GET /capabilities': _exposed('hwpx_capabilities', fields={}),
    'GET /observation-viewer': _excluded(_VIEWER),
    'GET /observation-viewer/session': _excluded(_VIEWER),
    'GET /observation-viewer/frame/latest.png': _excluded(_VIEWER),
    'GET /observation-viewer/frame/latest.json': _excluded(_VIEWER),
    'GET /observation-viewer/stream.mjpg': _excluded(_VIEWER),
    'GET /interactive/session/status': _exposed('hwpx_status (and session admission of every session tool)', fields={'session_id': 'session_id'}),
    'POST /interactive/session/open': _excluded(_LEDGER + '; hwpx_open opens managed sessions'),
    'POST /interactive/session/find': _excluded(_LEDGER),
    'POST /interactive/session/choose': _excluded(_LEDGER),
    'POST /interactive/session/enter': _excluded(_LEDGER),
    'POST /interactive/session/lock': _excluded(_LEDGER),
    'POST /interactive/session/verify-pre': _excluded(_LEDGER),
    'POST /interactive/session/apply': _excluded(_LEDGER),
    'POST /interactive/session/verify-post': _excluded(_LEDGER),
    'POST /interactive/session/undo': _excluded(_LEDGER + '; hwpx_command undo runs the native tracked Undo'),
    'POST /interactive/session/close': _excluded(_LEDGER + '; hwpx_close closes managed sessions'),
    'GET /interactive/session/{session_id}/verify-evidence/{step_name}/{recorded_at}': _excluded(_EVIDENCE),
    'GET /interactive/session/{session_id}/verify-evidence/{step_name}/{recorded_at}/frame': _excluded(_EVIDENCE),
    'GET /interactive/session/{session_id}/verify-evidence/{step_name}/{recorded_at}/frame.json': _excluded(_EVIDENCE),
    'POST /convert': _exposed('hwpx_job:convert', fields={'file': 'source_path'}),
    'POST /edit-and-convert': _exposed('hwpx_job:edit_and_convert', fields={'file': 'source_path', 'instructions_json': 'instructions'}),
    'POST /edit-and-convert-from-path': _equivalent('hwpx_job:edit_and_convert', _FROM_PATH),
    'POST /compile-authoring': _exposed('hwpx_job:compile_authoring', fields={
        'file': 'source_path', 'authoring_markdown': 'authoring_markdown', 'validation_json': 'validation',
        'cleanup_placeholders': 'cleanup_placeholders', 'policy_override_json': 'policy_override'}),
    'POST /compile-authoring-from-path': _equivalent('hwpx_job:compile_authoring', _FROM_PATH),
    'GET /jobs/{job_id}/confirm-state': _exposed('hwpx_job:confirm_state', fields={}),
    'GET /jobs/{job_id}/confirm-brief': _exposed('hwpx_job:confirm_brief', fields={}),
    'POST /jobs/{job_id}/confirm-action': _exposed('hwpx_job:confirm_action', fields={
        'action': 'confirm', 'authoring_markdown': 'authoring_markdown', 'validation_json': 'validation',
        'cleanup_placeholders': 'cleanup_placeholders', 'policy_override': 'policy_override', 'enqueue_convert': 'enqueue_convert'}),
    'POST /clear-placeholders': _exposed('hwpx_job:clear_placeholders', fields={'file': 'source_path', 'validation_json': 'validation'}),
    'POST /clear-placeholders-and-convert': _exposed('hwpx_job:clear_placeholders_and_convert', fields={'file': 'source_path', 'validation_json': 'validation'}),
    'POST /compile-placeholder-fills': _exposed('hwpx_job:compile_placeholder_fills', fields={
        'file': 'source_path', 'placeholder_fills_json': 'placeholder_fills', 'validation_json': 'validation', 'cleanup_placeholders': 'cleanup_placeholders'}),
    'POST /fill-placeholders-and-convert': _exposed('hwpx_job:fill_placeholders_and_convert', fields={
        'file': 'source_path', 'placeholder_fills_json': 'placeholder_fills', 'validation_json': 'validation', 'cleanup_placeholders': 'cleanup_placeholders'}),
    'POST /author-and-convert': _exposed('hwpx_job:author_and_convert', fields={
        'file': 'source_path', 'authoring_markdown': 'authoring_markdown', 'validation_json': 'validation',
        'cleanup_placeholders': 'cleanup_placeholders', 'policy_override_json': 'policy_override'}),
    'POST /author-and-convert-from-path': _equivalent('hwpx_job:author_and_convert', _FROM_PATH),
    'GET /jobs/{job_id}': _exposed('hwpx_job:status', fields={}),
    'GET /jobs/{job_id}/result': _exposed('hwpx_job:retrieve', fields={}, note='PDF downloaded, every page rendered to PNG'),
    'GET /jobs/{job_id}/edited': _exposed('hwpx_job:retrieve', fields={}, note='edited file downloaded and compared with the submitted source'),
    **{f'GET /jobs/{{job_id}}/{route}': _exposed(f'hwpx_job:metadata kind={kind}', fields={}) for kind, route in {
        'observation_status': 'observation-status', 'edit_summary': 'edit-summary', 'execution_result': 'execution-result',
        'validation_artifact': 'validation-artifact', 'validation_report': 'validation-report', 'bundle_manifest': 'bundle-manifest',
        'evidence_event': 'evidence-event', 'evidence_index': 'evidence-index', 'failure_summary': 'failure-summary',
        'runtime_status': 'runtime-status'}.items()},
}

# --------------------------------------------------------------------------- queued edit operations
_QUEUED = 'hwpx_job:edit_and_convert'
QUEUED_OPS: dict[str, dict[str, Any]] = {
    **{op: _exposed(_QUEUED) for op in (
        'replace_all', 'replace_text_safe', 'replace_paragraph_safe', 'replace_paragraph_range_safe', 'paragraph_replace_native',
        'paragraph_range_replace_native', 'replace_between_anchors_safe', 'clone_text_style', 'clone_paragraph_shape',
        'clone_paragraph_layout', 'insert_after_text', 'insert_before_text', 'insert_at_document_end', 'style_text',
        'style_text_in_paragraph', 'align_paragraph', 'paragraph_shape', 'list_paragraph', 'replace_empty_native_list_scaffold',
        'cursor_replace_text', 'cursor_delete_range', 'cursor_insert_text', 'cursor_snapshot', 'control_delete_by_anchor',
        'table_cell_action', 'table_cell_clear_text', 'table_cell_replace_text', 'table_patch_cells')},
    'native_action': _excluded('runs any HAction name with any parameter set (run/execute/auto); the backend has no allowlist for it, '
                               'so it is an unbounded escape hatch. The authoring compiler does not emit it, and compiled authoring plans '
                               'that carry it are refused before job instructions are written'),
    'page_number': {'status': 'backend_unsupported', 'reason': 'the queued runtime refuses it before any SDK work (require_queued_job_runtime_support); '
                    'use hwpx_command page_number_apply'},
    'official_numbering': {'status': 'backend_unsupported', 'reason': 'the queued runtime refuses it before any SDK work (require_queued_job_runtime_support); '
                           'use hwpx_command official_numbering_apply'},
}

QUEUED_EXCLUDED_FIELDS: dict[str, dict[str, str]] = {
    'cursor_snapshot': {'probe_identity_key': 'internal diagnostic switch for one historical fixture identity, not a caller option'},
}

# Outside backend parity: the CLI writes blank documents locally.
CLI_ONLY = {'hwpx new': 'local_cli_v1/hwpx_new.write_blank_hwpx writes a blank .hwpx on the CLI machine; no backend route exists'}


def matrix() -> dict[str, Any]:
    def rows(table, key):
        return [{key: name, **{k: v for k, v in row.items() if k != 'fields'}} for name, row in sorted(table.items())]

    tables = {'bundle_ops': rows(BUNDLE_OPS, 'backend'), 'rest_routes': rows(REST_ROUTES, 'backend'),
              'queued_ops': rows(QUEUED_OPS, 'backend')}
    counts = {name: {status: sum(1 for row in value if row['status'] == status) for status in
                     ('exposed', 'equivalent', 'excluded', 'backend_unsupported')} for name, value in tables.items()}
    return {**tables, 'counts': counts, 'excluded_fields': {'bundle': EXCLUDED_FIELDS, 'queued': QUEUED_EXCLUDED_FIELDS},
            'cli_only': CLI_ONLY, 'native_reference': NATIVE_REFERENCE}
