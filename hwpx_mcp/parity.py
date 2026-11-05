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
_UNDO_COUNTED = ('hwpx_command undo reverts the step as one request of the native editing actions it reports, compared with the document '
                 'state recorded before it, and redo compares with the state after it (local_cli_service._BUNDLE_UNDO_POLICY); a step whose '
                 'states could not both be recorded refuses undo_not_counted')
_UNDO_VARIANTS = ('; undo is counted per variant (each variant row: undo counted with its measured native Undo steps and cases, '
                  'not_counted, or not_applicable); a step of a not_counted variant refuses undo_not_counted before any native history call')
_UNDO_REDO_OBJECT = ('; redo is compared exactly, except that the one object an insertion re-created may differ in the attributes it was '
                     'measured to renew, which the reply names (an equation\'s z-order; a line, rectangle or ellipse also its drawing instance id, '
                     'an ellipse also its unused arc points; a header or footer its series number)')
_UNDO_FIELDS = ('; hyperlink and memo insertions always refuse it: their field ends make the control walk return an identifier that is '
                'not exact text, so the document cannot be compared')
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
    'text_replace_exact': _exposed(SESSION_TOOLS, note='one exact range of existing body or table-cell text, found by its own text, '
                                                       'occurrence or paragraph; the new text keeps the replaced text\'s format; ' + _UNDO_COUNTED),
    'table_cell_text_exact': _exposed(SESSION_TOOLS, note='the whole text of cells of one table bound by table_id+expected_hash, each with its '
                                                          'expected current text; returns the next binding; ' + _UNDO_COUNTED +
                                                          '; measured on Hancom 2024 up to the 100-cell maximum (200 native Undo steps, then as many Redo)'),
    'header_footer_inventory': _exposed(SESSION_TOOLS, note='read-only list of the existing headers and footers: section, apply_to (both, odd or even pages), '
                                                            'text, format, page number fields, the sections that show each, and the hash that binds it'),
    'header_footer_text_exact': _exposed(SESSION_TOOLS, note='one exact range of the text of one existing header or footer bound by kind+section+apply_to+'
                                                             'expected_area_hash and the exact expected_affected_sections array for shared owners, in the replaced text\'s format; its page number fields, the body and every other '
                                                             'area stay; returns the next hash; history is measured for one actually dirty step, not multi-dirty header/footer bundles; ' + _UNDO_COUNTED),
    'header_footer_format_exact': _exposed(SESSION_TOOLS, note='character format of exact text and paragraph format of its paragraph in one existing header or '
                                                               'footer, bound as header_footer_text_exact; history is measured for one actually dirty step only; a bundle with multiple dirty steps and any header/footer mutation refuses Undo/Redo before native calls; ' + _UNDO_COUNTED),
    'control_inventory': _exposed(SESSION_TOOLS, note='read-only native control list with target_id/expected_hash'),
    'picture_inventory': _exposed(SESSION_TOOLS, note='read-only structural picture census with exact SHAPEOBJECT identity/hash, frame, crop, media and explicit unqualified edit/history bounds'),
    'picture_replace_exact': _exposed(SESSION_TOOLS, note='implemented body-picture PNG replacement, same decoded aspect ratio, current GetCtrlInstID mapping before selection; final native qualification incomplete; Undo/Redo refused before dispatch'),
    'picture_crop_exact': _exposed(SESSION_TOOLS, note='implemented normalized 0..1 native source crop, same frame and payload; native edit/history/save/reopen qualification incomplete; Undo/Redo refused before dispatch'),
    'picture_layout_exact': _exposed(SESSION_TOOLS, note='implemented inline/square body-picture placement without resize; native edit/history/save/reopen qualification incomplete; Undo/Redo refused before dispatch'),
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
    'layout_exact': _exposed(SESSION_TOOLS, note=f'page_setup, columns (apply_to current_section; from_caret_new is refused before any edit with '
                                                f'columns_from_caret_new_unsupported), section_insert (only between two characters of one paragraph\'s text), '
                                                f'section_delete, hanging_indent; native qualification per variant in variants; {_UNDO_COUNTED}{_UNDO_VARIANTS}; '
                                                f'redo is compared exactly'),
    'layout_inspect': _exposed(SESSION_TOOLS, note='read-only page and column definition at the caret'),
    'object_insert_exact': _exposed(SESSION_TOOLS, note=f'footnote, endnote, memo, hyperlink, bookmark, equation, line, rectangle, ellipse, header, footer; '
                                                       f'native qualification per variant in variants; {_UNDO_COUNTED}{_UNDO_VARIANTS}{_UNDO_REDO_OBJECT}{_UNDO_FIELDS}'),
    'hwp_action': _exposed(SESSION_TOOLS, note='only the backend command-bundle navigation/selection allowlist'),
    'pyhwpx_call': _exposed(SESSION_TOOLS, note='only the backend command-bundle read/caret allowlist'),
    'export_pdf': _equivalent('hwpx_proof', 'hwpx_proof page/review runs the same backend export, then downloads, renders and checks the PDF; a bare bundle export leaves the PDF unretrieved'),
    'save_document': _equivalent('hwpx_save', 'hwpx_save runs the backend save and copies, verifies and records the saved file that hwpx_close relies on; a bare bundle save bypasses that retrieval'),
}

# Backend ops with a semantic pre-edit refusal path (local_cli_service._GUARDED_BUNDLE_OPS); every other
# exposed op reports a backend error as an unknown outcome that holds the session.
GUARDED = frozenset({'page_number_apply', 'official_numbering_apply', 'paragraph_insert_exact', 'paragraph_remove_exact',
                     'character_format_exact', 'paragraph_format_exact', 'table_inventory', 'table_read', 'table_edit_exact',
                     'table_style_exact', 'table_width_exact', 'table_create_exact', 'text_replace_exact', 'table_cell_text_exact',
                     'header_footer_inventory', 'header_footer_text_exact', 'header_footer_format_exact',
                     'picture_inventory', 'picture_replace_exact', 'picture_crop_exact', 'picture_layout_exact'})
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
                    'rendered output of the variant was checked from the saved bytes against the opened document, in the evidence '
                    'cases the row names; qualified_unaffected = that evidence comes from an earlier source and nothing on the '
                    'variant\'s code path changed since; not_qualified = the backend runs the variant but no such evidence exists for '
                    'this source; unsupported = refused before any edit with the named refusal_code. It does not cover other hosts, '
                    'Hancom versions or documents.')
NATIVE_STATUSES = ('qualified', 'qualified_unaffected', 'not_qualified', 'unsupported')


UNDO_STATUSES = ('counted', 'not_counted', 'not_applicable')


def _variant(kind: str, qualifier: str | None, scope: str, native: str, *, evidence: tuple[str, ...] = (), undo: str | None = None,
             undo_steps: int | None = None, undo_evidence: tuple[str, ...] = (), undo_reason: str | None = None, **extra: str) -> dict[str, Any]:
    """One variant row. A qualified or qualified_unaffected row names its evidence cases (stable scenario ids). An insertion or
    layout row also says how `hwpx_command undo` treats it: counted (its measured native Undo steps and the cases that measured
    them), not_counted (refused undo_not_counted before any native history call, with the reason) or not_applicable (the
    variant is refused before any edit)."""
    assert native in NATIVE_STATUSES
    assert bool(evidence) == (native in ('qualified', 'qualified_unaffected')), (kind, qualifier, native, evidence)
    row: dict[str, Any] = {'variant': kind if qualifier is None else f'{kind}:{qualifier}', 'kind': kind, 'scope': scope, 'native': native, **extra}
    if evidence:
        row['evidence'] = list(evidence)
    if undo is not None:
        assert undo in UNDO_STATUSES
        assert (undo == 'counted') == (type(undo_steps) is int and undo_steps >= 1 and bool(undo_evidence)), (kind, qualifier, undo)
        assert (undo == 'not_counted') == bool(undo_reason), (kind, qualifier, undo)
        assert undo != 'not_applicable' or native == 'unsupported', (kind, qualifier, undo)
        row['undo'] = undo
        if undo == 'counted':
            row.update(undo_steps=undo_steps, undo_evidence=list(undo_evidence))
        if undo_reason:
            row['undo_reason'] = undo_reason
    return row


def _counted(case: str, steps: int) -> dict[str, Any]:
    """A variant whose native Undo was measured on this source in ``case`` (which also saved, reopened and rendered it)."""
    return {'evidence': (case,), 'undo': 'counted', 'undo_steps': steps, 'undo_evidence': (case,)}


# Evidence cases are stable scenario ids of a native campaign (saved, reopened and rendered outputs checked from the saved
# bytes against the opened document). qualified_unaffected rows rest on cases of an earlier source whose path is unchanged.
# Drawn shapes go in with the characterized drawing contract (point layout and a solid 0.3 mm outline) and are verified
# from the readback: frame, transforms, drawn geometry and visible outline. A diagonal line has no characterized
# contract and is refused before any edit. Undo steps are app.object_insert.UNDO_STEPS and app.layout_ops.UNDO_STEPS.
_SHAPE_DRAWN = 'drawn with its points and a solid 0.3 mm black outline'
_FIELD_UNDO = 'its field ends make the control walk return an identifier that is not exact text, so the document cannot be compared'
_NOT_APPLICABLE = {'undo': 'not_applicable'}

VARIANTS: dict[str, list[dict[str, Any]]] = {
    'layout_exact': [
        _variant('page_setup', 'margins', 'margin and header/footer/gutter length fields, apply_to current_section', 'qualified', **_counted('undo_layout_margins', 1)),
        _variant('page_setup', 'landscape', 'landscape true, apply_to current_section', 'qualified', **_counted('undo_layout_landscape', 1)),
        _variant('page_setup', 'paper_size', 'paper_width_mm/paper_height_mm', 'qualified', **_counted('undo_layout_paper_size', 1)),
        _variant('page_setup', 'whole_document', 'apply_to whole_document', 'qualified', **_counted('undo_layout_whole_document', 1)),
        _variant('columns', 'current_section', 'count/gap_mm/same_width, apply_to current_section', 'qualified', **_counted('undo_layout_columns', 1)),
        _variant('columns', 'from_caret_new', 'apply_to from_caret_new (new columns from the caret): refused before any SDK call, because on '
                 'Hancom 2024 the native action changed the section\'s existing column definition instead of adding one at the caret',
                 'unsupported', refusal_code='columns_from_caret_new_unsupported', refusal_evidence=['columns_from_caret_new_refusal'], **_NOT_APPLICABLE),
        _variant('section_insert', 'inside_paragraph', 'caret between two characters of one paragraph\'s text; the two-section '
                 'document saved, reopened and rendered', 'qualified', **_counted('undo_layout_section_insert', 1)),
        _variant('section_insert', 'paragraph_start_or_end', 'caret at a paragraph\'s first text (also after its leading section '
                 'and column definitions), at its end, next to an inline control or in a paragraph without text', 'unsupported',
                 refusal_code='section_insert_position_unsupported', **_NOT_APPLICABLE),
        _variant('section_delete', None, 'caret at the first text of section_index in a saved multi-section document opened on its own',
                 'qualified', **_counted('undo_layout_section_delete', 1)),
        _variant('hanging_indent', None, 'marker_text at the paragraph start', 'qualified', **_counted('undo_layout_hanging', 1)),
    ],
    'object_insert_exact': [
        _variant('footnote', None, 'text at the caret', 'qualified', **_counted('undo_footnote', 2)),
        _variant('endnote', None, 'text at the caret', 'qualified', **_counted('undo_endnote', 2)),
        _variant('memo', None, 'text at the caret, no selection', 'qualified', evidence=('undo_memo_refused',), undo='not_counted', undo_reason=_FIELD_UNDO),
        _variant('memo', 'selection', 'text on a live selection', 'qualified', evidence=('undo_memo_selection_refused',), undo='not_counted', undo_reason=_FIELD_UNDO),
        _variant('hyperlink', None, 'url on the live selection of display_text', 'qualified', evidence=('undo_hyperlink_refused',), undo='not_counted', undo_reason=_FIELD_UNDO),
        _variant('bookmark', None, 'new name at the caret', 'qualified', **_counted('undo_bookmark', 1)),
        _variant('equation', None, 'script at the caret', 'qualified', **_counted('undo_equation', 1)),
        *(_variant('line', f'{axis}_{place}', f'{"height_mm" if axis == "horizontal" else "width_mm"} 0, treat_as_char {str(place == "inline").lower()}; {_SHAPE_DRAWN}',
                   'qualified', **_counted(f'undo_line_{axis}_{place}', 1)) for axis in ('horizontal', 'vertical') for place in ('inline', 'floating')),
        _variant('line', 'diagonal', 'width_mm and height_mm both non-zero: no characterized drawing contract, refused before any edit',
                 'unsupported', refusal_code='unverifiable_layout', **_NOT_APPLICABLE),
        *(_variant(shape, place, f'treat_as_char {str(place == "inline").lower()}; {_SHAPE_DRAWN}', 'qualified', **_counted(f'undo_{shape}_{place}', 1))
          for shape in ('rectangle', 'ellipse') for place in ('inline', 'floating')),
        *(_variant(kind, pages, f'apply_to {pages}', 'qualified', **_counted(f'undo_{kind}_{pages}', 2)) for kind in ('header', 'footer') for pages in ('both', 'odd', 'even')),
    ],
    'text_replace_exact': [
        _variant('body', 'single_format', 'one range of one character format in a body paragraph, found by text, match, occurrence or paragraph',
                 'qualified', evidence=('text_annual_plan',)),
        _variant('body', 'last_character', 'a range over several character formats with format_from last_character', 'qualified_unaffected', evidence=('text_replace_cases',)),
        _variant('body', 'mixed_formats', 'a range over several character formats without format_from', 'unsupported', refusal_code='target_mixed_format'),
        _variant('table_cell', None, 'one range in a table cell paragraph', 'qualified_unaffected', evidence=('text_replace_cases',)),
        _variant('other_context', None, 'text in a text box, caption or note', 'unsupported', refusal_code='target_unsupported_context'),
        _variant('header_footer', None, 'text in a header or footer: header_footer_text_exact edits it', 'unsupported', refusal_code='target_in_header_footer'),
    ],
    'header_footer_text_exact': [
        _variant('area', 'both', 'one range of one character format in the header or footer for every page of a section', 'qualified',
                 evidence=('hf_text_header_both', 'hf_text_footer_page_field'), undo='counted', undo_steps=2, undo_evidence=('undo_hf_text',)),
        _variant('area', 'odd_even', 'one range in the odd-page or even-page header or footer of a section, the other one untouched', 'qualified',
                 evidence=('hf_text_odd_section',), undo='counted', undo_steps=2, undo_evidence=('undo_hf_text',)),
        _variant('area', 'inherited', 'a section without its own area of this kind: refused, naming the section whose area it shows', 'unsupported',
                 refusal_code='area_inherited', **_NOT_APPLICABLE),
        _variant('area', 'missing_scope', 'a page scope the section does not define, or no such area at all', 'unsupported', refusal_code='area_not_found', **_NOT_APPLICABLE),
        _variant('area', 'duplicate_scope', 'two areas of one kind for the same pages of one section', 'unsupported', refusal_code='area_ambiguous', **_NOT_APPLICABLE),
        _variant('area', 'stale', 'an area changed since header_footer_inventory', 'unsupported', refusal_code='area_hash_mismatch', **_NOT_APPLICABLE),
        _variant('range', 'page_number_field', 'a range over the page number field', 'unsupported', refusal_code='target_crosses_control', **_NOT_APPLICABLE),
        _variant('range', 'mixed_formats', 'a range over several character formats without format_from', 'unsupported', refusal_code='target_mixed_format', **_NOT_APPLICABLE),
        _variant('range', 'inside_object', 'text in a table or text box inside the header or footer', 'unsupported', refusal_code='target_unsupported_context', **_NOT_APPLICABLE),
    ],
    'header_footer_format_exact': [
        _variant('character', None, 'bold, italic, underline, size, color or font over one exact range of one header or footer paragraph', 'qualified',
                 evidence=('hf_format_header',), undo='counted', undo_steps=1, undo_evidence=('undo_hf_format',)),
        _variant('paragraph', None, 'alignment, indents or line spacing of one header or footer paragraph', 'qualified',
                 evidence=('hf_format_even_paragraph',), undo='counted', undo_steps=1, undo_evidence=('undo_hf_format',)),
        _variant('both', None, 'character and paragraph properties in one step: two native actions, one counted undo of both', 'qualified',
                 evidence=('hf_format_header',), undo='counted', undo_steps=2, undo_evidence=('undo_hf_format',)),
        _variant('area', 'inherited', 'a section without its own area of this kind', 'unsupported', refusal_code='area_inherited', **_NOT_APPLICABLE),
        _variant('range', 'page_number_field', 'a range over the page number field', 'unsupported', refusal_code='target_crosses_control', **_NOT_APPLICABLE),
    ],
    'table_cell_text_exact': [
        _variant('single_paragraph', None, 'the whole text of cells of one paragraph and one character format, a merged range by its anchor cell',
                 'qualified', evidence=('text_annual_plan', 'undo_cells_100', 'cell_merged_anchor')),
        _variant('blank', None, 'a blank cell', 'qualified_unaffected', evidence=('cell_text_cases',)),
        _variant('multi_paragraph', None, 'a cell of several paragraphs', 'unsupported', refusal_code='cell_multi_paragraph'),
        _variant('mixed_format', None, 'a cell with text in several character formats', 'unsupported', refusal_code='cell_mixed_format'),
        _variant('objects', None, 'a cell holding a table, picture or other object', 'unsupported', refusal_code='cell_has_objects'),
    ],
}
for _op, _variants in VARIANTS.items():
    BUNDLE_OPS[_op]['variants'] = _variants

# These rows expose implemented callers, not a fabricated native acceptance.
for _op, _kinds in {'picture_replace_exact': ('png_same_aspect',),
                    'picture_crop_exact': ('normalized_source',),
                    'picture_layout_exact': ('inline', 'square')}.items():
    BUNDLE_OPS[_op]['variants'] = [
        _variant(_kind, None, 'existing body picture without a native caption', 'not_qualified',
                 undo='not_counted', undo_reason='native picture edit/Undo/Redo/save/reopen qualification is incomplete; '
                 'only an explicitly configured pinned HWPX profile that declares this kind counts it, against recorded package states',
                 preservation='legacy HWPML strict comparison by default; complete native HWPX private-copy package proof, '
                 'verified public save and saved-package proof only under an explicitly configured pinned profile (prospective, '
                 'reported with its literal qualification); legacy HWPML BINDATA Size is never certified')
        for _kind in _kinds
    ]

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
