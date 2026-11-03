"""Closed MCP models for the backend's queued job, template and placeholder routes.

Operation fields follow what the queued edit runtime reads (app/edit_ops.py
apply_edit_operations and its helpers); tests/test_mcp_backend_parity.py
extracts those reads from source and fails when a field is missing here.
`native_action` (an unbounded HAction/parameter-set runner) and the native
numbering operations (refused by the queued runtime itself) are deliberately
absent.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, model_validator

from hwpx_mcp.operations import Closed, CursorPos

JobId = Annotated[str, Field(pattern=r'^[a-f0-9]{32}$', description='A job id returned by an hwpx_job submit action of this adapter.')]
SourcePath = Annotated[str, Field(min_length=1, max_length=4096, description='Absolute .hwpx/.hwp path under the input or artifact root.')]
Find = Annotated[str, Field(min_length=1, max_length=4000)]
Body = Annotated[str, Field(max_length=20000)]
Short = Annotated[str, Field(min_length=1, max_length=500)]
Color = Annotated[str, Field(pattern=r'^#?(0x)?[0-9A-Fa-f]{6}$')] | Annotated[int, Field(ge=0, le=0xFFFFFF)]
Expect = Short | Annotated[list[Short], Field(min_length=1, max_length=50)]
Fingerprint = Short | dict[str, Any]


class _Queued(Closed):
    step_id: Short | None = None
    verification_mode: Literal['text-local', 'text-broader', 'image'] | None = None
    needs_visual_verification: bool | None = None
    verification: dict[str, Any] | None = Field(default=None, description='Record-only; copied into the step summary.')
    metadata: dict[str, Any] | None = Field(default=None, description='Record-only; copied into the step summary.')
    allow_zero_match: bool | None = Field(default=None, description='Record-only marker counted in the evidence summary.')


class _Search(_Queued):
    apply: Literal['all', 'first'] | None = Field(default=None, description='Backend default: all.')
    match_case: bool | None = None
    whole_word: bool | None = None


class _Packed(_Search):
    allow_packed_paragraph: bool | None = None


class ReplaceAll(_Packed):
    op: Literal['replace_all']
    find: Find
    replace: Body | None = None
    regex: Literal[False] | None = Field(default=None, description='Regex mode is refused by the backend.')


class ReplaceTextSafe(_Packed):
    op: Literal['replace_text_safe']
    find: Find
    replace: Body | None = None


class _ParagraphReplace(_Packed):
    find: Find
    replace: Annotated[str, Field(min_length=1, max_length=20000)]
    expected_present_after: Short | None = None
    expected_absent_after: Short | None = None
    keep_paragraph_break: bool | None = Field(default=None, description=(
        'true: replace only the paragraph\'s content, so its paragraph break and the next paragraph (with its shape) are kept. '
        'Default (false): the paragraph is selected together with its break, as described above.'))


class ReplaceParagraphSafe(_ParagraphReplace):
    """Replace one whole paragraph. The backend selects the paragraph that contains `find` together with its paragraph break, deletes it and types `replace`. End `replace` with "\r\n" to keep that break, or set keep_paragraph_break=true to replace only the content; otherwise the next paragraph joins the replacement and the job still succeeds (measured on Hancom 2024; the last paragraph has no break to delete)."""
    op: Literal['replace_paragraph_safe']


class ParagraphReplaceNative(_ParagraphReplace):
    """Replace one whole paragraph through the native path. The backend selects the paragraph that contains `find` together with its paragraph break, deletes it and types `replace`. End `replace` with "\r\n" to keep that break, or set keep_paragraph_break=true to replace only the content; otherwise the next paragraph joins the replacement and the job still succeeds (measured on Hancom 2024; the last paragraph has no break to delete)."""
    op: Literal['paragraph_replace_native']


class _RangeReplace(_Packed):
    start_find: Find
    end_find: Find
    replace: Annotated[str, Field(min_length=1, max_length=20000)]


class ReplaceParagraphRangeSafe(_RangeReplace):
    op: Literal['replace_paragraph_range_safe']


class ParagraphRangeReplaceNative(_RangeReplace):
    op: Literal['paragraph_range_replace_native']


class ReplaceBetweenAnchorsSafe(_Packed):
    op: Literal['replace_between_anchors_safe']
    start_anchor: Find
    end_anchor: Find
    replace: Body | None = None


class _Clone(_Search):
    source_find: Find
    find: Find


class CloneTextStyle(_Clone):
    op: Literal['clone_text_style']


class CloneParagraphShape(_Clone):
    op: Literal['clone_paragraph_shape']


class CloneParagraphLayout(_Clone):
    op: Literal['clone_paragraph_layout']


class InsertAfterText(_Packed):
    op: Literal['insert_after_text']
    find: Find
    insert: Annotated[str, Field(min_length=1, max_length=20000)]


class InsertBeforeText(_Packed):
    op: Literal['insert_before_text']
    find: Find
    insert: Annotated[str, Field(min_length=1, max_length=20000)]


class InsertAtDocumentEnd(_Queued):
    op: Literal['insert_at_document_end']
    insert: Annotated[str, Field(min_length=1, max_length=20000)]
    new_paragraph_before: bool | None = None


class _TextStyle(_Search):
    face_name: Short | None = None
    height_pt: float | None = Field(default=None, gt=0, le=4096)
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    underline_shape: int | None = Field(default=None, ge=0, le=20)
    strikeout: bool | None = None
    text_color_rgb: Color | None = None
    shade_color_rgb: Color | None = None
    spacing: int | None = Field(default=None, ge=-50, le=50)
    ratio: int | None = Field(default=None, ge=50, le=200)

    @model_validator(mode='after')
    def require_a_style(self):
        names = ('face_name', 'height_pt', 'bold', 'italic', 'underline', 'strikeout', 'text_color_rgb', 'shade_color_rgb', 'spacing', 'ratio')
        if all(getattr(self, name) is None for name in names):
            raise ValueError('give at least one style field')
        return self


class StyleText(_TextStyle):
    op: Literal['style_text']
    find: Find


class StyleTextInParagraph(_TextStyle):
    op: Literal['style_text_in_paragraph']
    paragraph_find: Find
    text: Find
    occurrence: int | None = Field(default=None, ge=1)


Align = Literal['left', 'right', 'center', 'justify', 'distribute', 'distribute_space']


class AlignParagraph(_Search):
    op: Literal['align_paragraph']
    find: Find
    align: Align


class ParagraphShape(_Search):
    op: Literal['paragraph_shape']
    find: Find
    align: Align | None = None
    line_spacing: int | None = Field(default=None, ge=0, le=1000)
    indentation: float | None = None
    left_margin: float | None = None
    right_margin: float | None = None
    prev_spacing: float | None = None
    next_spacing: float | None = None
    condense: int | None = Field(default=None, ge=0, le=100)
    text_alignment: int | None = Field(default=None, ge=0, le=10)
    pagebreak_before: bool | None = None
    keep_lines_together: bool | None = None
    keep_with_next: bool | None = None
    widow_orphan: bool | None = None
    line_wrap: bool | None = None
    font_line_height: bool | None = None
    auto_space_easian_num: bool | None = None
    auto_space_easian_eng: bool | None = None
    snap_to_grid: bool | None = None
    break_non_latin_word: int | None = Field(default=None, ge=0, le=10)

    @model_validator(mode='after')
    def require_an_option(self):
        if all(getattr(self, name) is None for name in type(self).model_fields if name not in set(_Search.model_fields) | {'op', 'find'}):
            raise ValueError('paragraph_shape requires at least one paragraph option')
        return self


ListKind = Literal['none', 'bullet', 'number', 'outline']


class ListParagraph(_Search):
    op: Literal['list_paragraph']
    find: Find
    kind: ListKind
    level: int | None = Field(default=None, ge=1, le=10)
    source_find: Find | None = None


class ScaffoldItem(Closed):
    text: Find
    kind: ListKind | None = None
    level: int | None = Field(default=None, ge=1, le=10)


class ReplaceEmptyNativeListScaffold(_Search):
    op: Literal['replace_empty_native_list_scaffold']
    find: Find | None = None
    cursor_pos: CursorPos | None = None
    items: Annotated[list[Find | ScaffoldItem], Field(min_length=1, max_length=200)] | None = None
    replace: Annotated[str, Field(min_length=1, max_length=20000)] | None = None
    kind: ListKind | None = None
    level: int | None = Field(default=None, ge=1, le=10)
    allow_typed_markers: bool | None = None
    require_empty_scaffold: bool | None = Field(default=None, description='Refuse unless the target paragraph is empty. Give cursor_pos with it: a paragraph found by visible text is never empty.')
    forbidden_concatenations: Expect | None = None
    source_find: Find | None = None

    @model_validator(mode='after')
    def target_and_items(self):
        if self.find is None and self.cursor_pos is None:
            raise ValueError("give find or cursor_pos")
        if self.require_empty_scaffold and self.find is not None and self.find.split():
            raise ValueError('require_empty_scaffold checks that the target paragraph is empty; give cursor_pos, because a paragraph found by visible text is never empty')
        if self.items is None and self.replace is None:
            raise ValueError("give items or a multiline replace")
        return self


class CursorReplaceText(_Queued):
    op: Literal['cursor_replace_text']
    start_cursor_pos: CursorPos
    end_cursor_pos: CursorPos
    replace: Body | None = None
    expected_selected_text: Body | None = None


class CursorDeleteRange(_Queued):
    op: Literal['cursor_delete_range']
    start_cursor_pos: CursorPos
    end_cursor_pos: CursorPos
    expected_selected_text: Body | None = None


class CursorInsertText(_Queued):
    op: Literal['cursor_insert_text']
    cursor_pos: CursorPos
    insert: Body | None = None
    delete_selection: bool | None = None


class CursorSnapshot(_Search):
    op: Literal['cursor_snapshot']
    find: Find | None = None
    cursor_pos: CursorPos | None = None
    expected_cell_addr: Short | None = None
    expected_table_fingerprint: dict[str, Any] | None = None
    select_paragraph: bool | None = None
    capture_selected_text: bool | None = None
    expected_selected_text_absent_any: Expect | None = None
    expected_selected_text_present_any: Expect | None = None


class ControlDeleteByAnchor(_Search):
    op: Literal['control_delete_by_anchor']
    target_ctrl_id: Short
    find: Find | None = None
    cursor_pos: CursorPos | None = None
    expected_cell_addr: Short | None = None
    anchor_option: int | None = None
    max_controls: int | None = Field(default=None, ge=1, le=100000)
    matching_target_ordinal_anywhere: int | None = Field(default=None, ge=1)
    require_in_cell: bool | None = None


class TableCellAction(_Search):
    op: Literal['table_cell_action']
    action: Literal['left', 'right', 'up', 'down', 'block', 'block_row', 'block_col', 'extend']
    find: Find | None = None
    cursor_pos: CursorPos | None = None
    expected_cell_addr: Short | None = None
    require_in_cell: bool | None = None
    select_paragraph: bool | None = None
    expected_selection_mode: int | None = None
    expected_is_cell: bool | None = None


class _CellTarget(_Queued):
    cursor_pos: CursorPos | None = None
    cursor_pos_from_snapshot: Literal['last'] | None = None
    expected_cell_addr: Short | None = None
    expected_cell_addr_from_snapshot: Literal['last'] | None = None
    cell_addr: Short | None = None
    require_in_cell: bool | None = None
    expected_selection_mode: int | None = None
    expected_is_cell: bool | None = None


class TableCellClearText(_CellTarget):
    op: Literal['table_cell_clear_text']


class TableCellReplaceText(_CellTarget):
    op: Literal['table_cell_replace_text']
    apply: Literal['all', 'first'] | None = None
    match_case: bool | None = None
    whole_word: bool | None = None
    find: Find | None = None
    expected_table_fingerprint: dict[str, Any] | None = None
    replace: Body | None = None
    expected_selected_text: Body | None = None
    select_paragraph: bool | None = None
    disable_put_field_text: bool | None = None
    capture_selected_text_after: bool | None = None
    expected_selected_text_absent_any_after: Expect | None = None
    expected_selected_text_present_any_after: Expect | None = None


class CellPatch(Closed):
    cell_addr: Short
    replace: Body | None = None


class TablePatchCells(_Queued):
    op: Literal['table_patch_cells']
    entry_find: Find | None = None
    cursor_pos: CursorPos | None = None
    expected_entry_cell_addr: Short | None = None
    cell_addr: Short | None = None
    patches: Annotated[list[CellPatch], Field(min_length=1, max_length=500)]
    match_case: bool | None = None
    whole_word: bool | None = None
    require_in_cell: bool | None = None
    expected_table_fingerprint: Fingerprint | None = None
    expected_selection_mode: int | None = None
    expected_is_cell: bool | None = None

    @model_validator(mode='after')
    def entry(self):
        if self.entry_find is None and self.cursor_pos is None:
            raise ValueError('give entry_find or cursor_pos')
        return self


QueuedOperation = Annotated[
    ReplaceAll | ReplaceTextSafe | ReplaceParagraphSafe | ParagraphReplaceNative | ReplaceParagraphRangeSafe
    | ParagraphRangeReplaceNative | ReplaceBetweenAnchorsSafe | CloneTextStyle | CloneParagraphShape | CloneParagraphLayout
    | InsertAfterText | InsertBeforeText | InsertAtDocumentEnd | StyleText | StyleTextInParagraph | AlignParagraph
    | ParagraphShape | ListParagraph | ReplaceEmptyNativeListScaffold | CursorReplaceText | CursorDeleteRange
    | CursorInsertText | CursorSnapshot | ControlDeleteByAnchor | TableCellAction | TableCellClearText
    | TableCellReplaceText | TablePatchCells,
    Field(discriminator='op'),
]


class JobValidation(Closed):
    preserve_page_count: bool | None = None
    max_page_count: int | None = Field(default=None, ge=1)
    max_page_increase: int | None = Field(default=None, ge=0)


class EditInstructions(Closed):
    operations: Annotated[list[QueuedOperation], Field(min_length=1, max_length=500)]
    validation: JobValidation | None = None
    request_id: Short | None = None
    intent_id: Short | None = None
    idempotency_key: Short | None = None
    template_version: Short | None = None
    inspect_snapshot_id: Short | None = None
    template_fingerprint: Short | None = None
    resolved_via: Short | None = None
    resolved_target_id: Short | None = None
    execution_run_id: Short | None = None
    validation_run_id: Short | None = None
    compile_status: Short | None = None
    failure_reason: Short | None = None
    precondition: dict[str, Any] | None = Field(default=None, description='Kept with the job instructions; the worker does not act on it.')
    metadata: dict[str, Any] | None = Field(default=None, description=(
        'Passed to the worker, which acts on known keys: post_serialization_proof or post_serialization_proofs run an extra check '
        'after saving that can fail the job; compile_warning_badges is copied into the validation artifact; tracking keys '
        '(request_id, intent_id, idempotency_key, template_version, inspect_snapshot_id, resolved_via, resolved_target_id, '
        'execution_run_id, validation_run_id) are read from here when not given at the top level. Other keys are kept with the instructions.'))


class PolicyOverride(Closed):
    decision_override: Literal['auto', 'confirm', 'manual'] | None = None
    shape_key: Short | None = None
    source: Short | None = None
    note: Annotated[str, Field(max_length=2000)] | None = None


FillValue = Annotated[str, Field(max_length=20000)] | int | float | bool | None
# The backend's token rules (app/template_engine.py canonicalize_placeholder_token / canonicalize_placeholder_segment):
# a key is a [[BR::...]] token or reduces to at least one ASCII letter/digit segment; a table name needs one too.
_PLACEHOLDER_TOKEN = re.compile(r'\[\[BR::[A-Za-z0-9_:\-]+\]\]')
_ASCII_ALNUM = re.compile(r'[A-Za-z0-9]')


def _placeholder_key(value: str) -> str:
    raw = value.strip()
    candidate = raw
    if candidate.startswith('[[BR::') and candidate.endswith(']]'):
        candidate = candidate[6:-2]
    elif candidate.startswith('BR::'):
        candidate = candidate[4:]
    if not raw or not (_PLACEHOLDER_TOKEN.fullmatch(raw) or _ASCII_ALNUM.search(candidate)):
        raise ValueError('a placeholder key is a [[BR::NAME]] token or a name with at least one ASCII letter or digit (for example NAME)')
    return value


def _table_name(value: str | None, where: str) -> None:
    if not value or not _ASCII_ALNUM.search(str(value).strip()):
        raise ValueError(f'{where} needs a name with at least one ASCII letter or digit; the backend builds [[BR::TABLE::NAME::Rnn_Cnn]] from it')


PlaceholderKey = Annotated[str, Field(min_length=1, max_length=500, description='[[BR::NAME]] or a name with ASCII letters/digits, such as NAME.'),
                           AfterValidator(_placeholder_key)]
CellKey = Annotated[str, Field(pattern=r'^[Rr]?(\d+)\s*[_\- ]?[Cc]?(\d+)$', description='r1c1, R01_C02 or similar.')]


class FillTable(Closed):
    name: Short | None = None
    table: Short | None = None
    scope: Short | None = None
    cells: dict[CellKey, FillValue]

    @model_validator(mode='after')
    def named(self):
        if not (self.name or self.table or self.scope):
            raise ValueError('a table needs name, table or scope')
        _table_name(self.name or self.table or self.scope, 'a table')
        return self


class FillTableCell(Closed):
    name: Short | None = None
    table: Short | None = None
    scope: Short | None = None
    row: int = Field(ge=1)
    col: int = Field(ge=1)
    value: FillValue = None

    @model_validator(mode='after')
    def named(self):
        if not (self.name or self.table or self.scope):
            raise ValueError('a table cell needs name, table or scope')
        _table_name(self.table or self.name or self.scope, 'a table cell')
        return self


class PlaceholderFills(Closed):
    placeholders: dict[PlaceholderKey, FillValue] | None = None
    tables: Annotated[list[FillTable], Field(max_length=200)] | None = None
    table_cells: Annotated[list[FillTableCell], Field(max_length=5000)] | None = None

    @model_validator(mode='after')
    def not_empty(self):
        if not (self.placeholders or self.tables or self.table_cells):
            raise ValueError('give placeholders, tables or table_cells')
        return self


Markdown = Annotated[str, Field(min_length=1, max_length=200000)]
Cleanup = Literal['none', 'unused', 'all']


class JobConvert(Closed):
    action: Literal['convert']
    source_path: SourcePath


class JobEditAndConvert(Closed):
    action: Literal['edit_and_convert']
    source_path: SourcePath
    instructions: EditInstructions


class _Authoring(Closed):
    source_path: SourcePath
    authoring_markdown: Markdown
    validation: JobValidation | None = None
    cleanup_placeholders: Cleanup | None = None
    policy_override: PolicyOverride | None = None


class JobCompileAuthoring(_Authoring):
    """Compile authoring markdown into a plan that waits for confirm_action. An exact target paragraph is replaced in its content only, so the paragraph after it (for example the next heading) keeps its text and shape."""
    action: Literal['compile_authoring']


class JobAuthorAndConvert(_Authoring):
    """Compile authoring markdown and run it as one queued job. An exact target paragraph is replaced in its content only, so the paragraph after it (for example the next heading) keeps its text and shape."""
    action: Literal['author_and_convert']


class _Clear(Closed):
    source_path: SourcePath
    validation: JobValidation | None = None


class JobClearPlaceholders(_Clear):
    action: Literal['clear_placeholders']


class JobClearPlaceholdersAndConvert(_Clear):
    action: Literal['clear_placeholders_and_convert']


class _Fills(Closed):
    source_path: SourcePath
    placeholder_fills: PlaceholderFills
    validation: JobValidation | None = None
    cleanup_placeholders: Cleanup | None = None


class JobCompilePlaceholderFills(_Fills):
    action: Literal['compile_placeholder_fills']


class JobFillPlaceholdersAndConvert(_Fills):
    action: Literal['fill_placeholders_and_convert']


class JobConfirmAction(Closed):
    action: Literal['confirm_action']
    job_id: JobId
    confirm: Literal['inspect_more', 'abort', 'regenerate', 'approve']
    authoring_markdown: Markdown | None = None
    validation: JobValidation | None = None
    cleanup_placeholders: Cleanup | None = None
    policy_override: PolicyOverride | None = None
    enqueue_convert: bool | None = None

    @model_validator(mode='after')
    def fields_the_action_reads(self):
        # /jobs/{job_id}/confirm-action reads these only for the named action and ignores them otherwise.
        reads = {'regenerate': {'authoring_markdown', 'validation', 'cleanup_placeholders', 'policy_override'},
                 'approve': {'enqueue_convert'}}.get(self.confirm, set())
        stray = sorted(name for name in ('authoring_markdown', 'validation', 'cleanup_placeholders', 'policy_override', 'enqueue_convert')
                       if getattr(self, name) is not None and name not in reads)
        if stray:
            raise ValueError(f'confirm={self.confirm} does not use: {", ".join(stray)}')
        if self.confirm == 'regenerate' and self.authoring_markdown is None:
            raise ValueError('regenerate requires authoring_markdown')
        return self


class JobStatus(Closed):
    action: Literal['status']
    job_id: JobId


class JobConfirmState(Closed):
    action: Literal['confirm_state']
    job_id: JobId


class JobConfirmBrief(Closed):
    action: Literal['confirm_brief']
    job_id: JobId


METADATA_ROUTES = {
    'edit_summary': 'edit-summary', 'execution_result': 'execution-result', 'validation_artifact': 'validation-artifact',
    'validation_report': 'validation-report', 'bundle_manifest': 'bundle-manifest', 'evidence_event': 'evidence-event',
    'evidence_index': 'evidence-index', 'failure_summary': 'failure-summary', 'runtime_status': 'runtime-status',
    'observation_status': 'observation-status',
}


class JobMetadata(Closed):
    action: Literal['metadata']
    job_id: JobId
    kind: Literal['edit_summary', 'execution_result', 'validation_artifact', 'validation_report', 'bundle_manifest',
                  'evidence_event', 'evidence_index', 'failure_summary', 'runtime_status', 'observation_status']


class JobRetrieve(Closed):
    action: Literal['retrieve']
    job_id: JobId
    dpi: int = Field(default=100, ge=72, le=300)
    max_pages: int = Field(default=30, ge=1, le=200)


JobRequest = Annotated[
    JobConvert | JobEditAndConvert | JobCompileAuthoring | JobAuthorAndConvert | JobClearPlaceholders
    | JobClearPlaceholdersAndConvert | JobCompilePlaceholderFills | JobFillPlaceholdersAndConvert | JobConfirmAction
    | JobStatus | JobConfirmState | JobConfirmBrief | JobMetadata | JobRetrieve,
    Field(discriminator='action'),
]

SUBMIT_ROUTES = {
    'convert': '/convert', 'edit_and_convert': '/edit-and-convert', 'compile_authoring': '/compile-authoring',
    'author_and_convert': '/author-and-convert', 'clear_placeholders': '/clear-placeholders',
    'clear_placeholders_and_convert': '/clear-placeholders-and-convert',
    'compile_placeholder_fills': '/compile-placeholder-fills', 'fill_placeholders_and_convert': '/fill-placeholders-and-convert',
}
