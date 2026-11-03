"""Closed MCP models for backend operations beyond the original exact set.

Field names, ranges and enums follow the backend command packages
(app/command_packages/commands/*), the local-CLI REST request models and the
queued edit runtime (app/edit_ops.py). The backend stays the authority: these
models only refuse what the backend would refuse anyway, and forward the rest
unchanged. tests/test_mcp_backend_parity.py checks them against the backend's
own manifests, constants and validators.

Only standard library and Pydantic are imported here, so the thin MCP client
needs no native, COM, FastAPI or backend modules.
"""
from __future__ import annotations

import math
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


Label = Annotated[str, Field(min_length=1, max_length=80)]
Text500 = Annotated[str, Field(min_length=1, max_length=500)]
Page = Annotated[int, Field(ge=1, le=10000)]
Count = Annotated[int, Field(ge=1, le=4096)]
CtrlId = Annotated[str, Field(min_length=6, max_length=500, pattern=r'^ctrl/\S+$',
                              description='A control identity from control_inventory or readback, such as ctrl/3/tbl/1.')]
TableCtrlId = Annotated[str, Field(min_length=12, max_length=500, pattern=r'^ctrl/[0-9]+/tbl/[^/]+$',
                                   description='A table control identity (ctrl/<n>/tbl/<id>) from control_inventory; other controls make the legacy table engines fail and hold the session.')]
CtrlHash = Annotated[str, Field(pattern=r'^sha256:[a-f0-9]{24,64}$', description='The control proof hash from control_inventory or readback.')]
Sha64 = Annotated[str, Field(pattern=r'^sha256:[a-f0-9]{64}$')]
Rgb = Annotated[str, Field(pattern=r'^#[0-9A-Fa-f]{6}$')]
HOLD_NOTE = "Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."
Pos3 = Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=3, max_length=3,
                 description='The caret position [list, para, pos] exactly as hwpx_where or a where step reports it.')]
CursorPos = Annotated[list[int], Field(min_length=3, max_length=3, description='[list, para, pos].')]
CellAddr = Annotated[str, Field(min_length=2, max_length=12, pattern=r'^[A-Za-z]{1,3}[0-9]{1,4}$')]


def _text_ok(value: str) -> bool:
    return bool(value.strip())


def _no_stray(model: BaseModel, allowed: set[str], context: str) -> None:
    stray = sorted(name for name in type(model).model_fields
                   if name not in allowed and name not in {'op', 'label'} and getattr(model, name) is not None)
    if stray:
        raise ValueError(f'{context} does not take: {", ".join(stray)}')


# --------------------------------------------------------------------------
# Control targets (legacy native control inventory identities)
# --------------------------------------------------------------------------

class _Scope(Closed):
    """Where the backend looks for the control: an anchor sentence and/or a page range."""
    section_anchor: Text500 | None = Field(default=None, description='A sentence that appears once near the control.')
    page_from: Page | None = None
    page_to: Page | None = None
    around: Text500 | None = Field(default=None, description='Optional nearby text that must also be found.')
    max_controls: Count | None = None

    @model_validator(mode='after')
    def require_scope(self):
        if self.section_anchor is None and self.page_from is None:
            raise ValueError('give section_anchor or page_from')
        if self.page_from is not None and self.page_to is not None and self.page_to < self.page_from:
            raise ValueError('page_to must be >= page_from')
        return self


class _Inventory(_Scope):
    target_id: Text500 | None = None
    expected_hash: Text500 | None = None
    expected_page: Page | None = None


class ControlInventoryStep(_Inventory):
    """Read-only list of native controls with target_id and proof hash. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['control_inventory']
    label: Label = 'mcp:control-inventory'


class TableFrameInventoryStep(_Inventory):
    """Read-only list of table frames. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['table_frame_inventory']
    label: Label = 'mcp:table-frame-inventory'


class _Control(_Scope):
    target_id: CtrlId
    expected_hash: CtrlHash
    expected_page: Page


class _TableControl(_Control):
    target_id: TableCtrlId


class ExactControlSelectProofStep(_Control):
    """Read-only proof that one control can be selected. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['exact_control_select_proof']
    label: Label = 'mcp:exact-control-select-proof'


class TableCellStructureStep(_TableControl):
    """Read-only cell structure of one table control. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['table_cell_structure_exact']
    label: Label = 'mcp:table-cell-structure-exact'


class ControlDeleteStep(_Control):
    """Delete one control found by its inventory identity. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['control_delete_exact']
    label: Label = 'mcp:control-delete-exact'
    confirm_remove: Literal[True]


class ControlMoveResizeStep(_Control):
    """Move and/or scale one control found by its inventory identity. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['control_move_resize_exact']
    label: Label = 'mcp:control-move-resize-exact'
    scale_percent: float | None = Field(default=None, ge=5.0, le=200.0, description='Scale the control to this percent of its size.')
    move_dx_mm: float | None = Field(default=None, ge=-300.0, le=300.0)
    move_dy_mm: float | None = Field(default=None, ge=-300.0, le=300.0)
    confirm_layout: Literal[True]

    @model_validator(mode='after')
    def require_a_change(self):
        if self.scale_percent is None and not (self.move_dx_mm or self.move_dy_mm):
            raise ValueError('give scale_percent or a non-zero move_dx_mm/move_dy_mm')
        return self


class ControlJoinPreviousStep(Closed):
    """Join the paragraph holding one control to the previous paragraph. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['control_join_previous_exact']
    label: Label = 'mcp:control-join-previous-exact'
    target_id: CtrlId
    expected_hash: CtrlHash
    expected_page: Page
    page_from: Page | None = None
    page_to: Page | None = None
    max_controls: Count | None = None
    delete_back_count: int | None = Field(default=None, ge=1, le=5)
    max_page_after: Page | None = None
    confirm_layout: Literal[True]


class CellRowFitStep(_TableControl):
    """Set the row height of one table control by exactly one selector. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['cell_row_fit_exact']
    label: Label = 'mcp:cell-row-fit-exact'
    row_height_percent: float | None = Field(default=None, ge=20.0, le=120.0)
    row_height_hu: float | None = Field(default=None, ge=1000.0, le=200000.0)
    row_height_mm: float | None = Field(default=None, ge=3.0, le=700.0)
    resize_up_steps: int | None = Field(default=None, ge=1, le=200)
    resize_down_steps: int | None = Field(default=None, ge=1, le=200)
    line_spacing: int | None = Field(default=None, ge=80, le=200)
    char_height_percent: float | None = Field(default=None, ge=70.0, le=110.0)
    confirm_layout: Literal[True]

    @model_validator(mode='after')
    def require_one_selector(self):
        selectors = ('row_height_percent', 'row_height_hu', 'row_height_mm', 'resize_up_steps', 'resize_down_steps',
                     'line_spacing', 'char_height_percent')
        if sum(getattr(self, name) is not None for name in selectors) != 1:
            raise ValueError('cell_row_fit_exact takes exactly one row height selector')
        return self


class TableSplitStep(_TableControl):
    """Split one table control in two below a row. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['table_split_exact']
    label: Label = 'mcp:table-split-exact'
    down_rows: int = Field(ge=1, le=200, description='Rows that stay in the first table; the rest become a new table below.')
    confirm_layout: Literal[True]


StructureAction = Literal['insert_row_above', 'insert_row_below', 'insert_col_left', 'insert_col_right',
                          'delete_row', 'delete_col', 'merge_cells', 'split_cell']


class TableStructureStep(_TableControl):
    """Insert or delete rows or columns, merge or split cells of one table control. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['table_structure_exact']
    label: Label = 'mcp:table-structure-exact'
    action: StructureAction
    row: int = Field(ge=1, le=500)
    col: int = Field(ge=1, le=500)
    end_row: int | None = Field(default=None, ge=1, le=500)
    end_col: int | None = Field(default=None, ge=1, le=500)
    count: int | None = Field(default=None, ge=1, le=50)
    split_rows: int | None = Field(default=None, ge=1, le=20)
    split_cols: int | None = Field(default=None, ge=1, le=20)
    expected_rows: int = Field(ge=1, le=500)
    expected_cols: int = Field(ge=1, le=500)
    confirm_layout: Literal[True]

    @model_validator(mode='after')
    def fields_of_the_action(self):
        allowed = {'merge_cells': {'end_row', 'end_col'}, 'split_cell': {'split_rows', 'split_cols'}}.get(
            self.action, {'count'})
        stray = [name for name in ('end_row', 'end_col', 'count', 'split_rows', 'split_cols')
                 if getattr(self, name) is not None and name not in allowed]
        if stray:
            raise ValueError(f'{self.action} does not take: {", ".join(stray)}')
        if self.action == 'merge_cells' and (self.end_row is None or self.end_col is None):
            raise ValueError('merge_cells requires end_row and end_col')
        if self.action == 'split_cell' and (self.split_rows or 1) * (self.split_cols or 1) < 2:
            raise ValueError('split_cell needs split_rows or split_cols of at least 2')
        return self


class TableColumnWidthStep(_TableControl):
    """Set column widths of one table control against a full set of expected values. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['table_column_width_exact']
    label: Label = 'mcp:table-column-width-exact'
    expected_preimage_sha256: Text500 | None = None
    expected_cell_inventory_hash: Text500 | None = None
    expected_document_text_hash: Text500 | None = None
    expected_bindata_manifest_hash: Text500 | None = None
    expected_text_char_count: int | None = Field(default=None, ge=1)
    expected_nonempty_line_count: int | None = Field(default=None, ge=1)
    expected_rows: int | None = Field(default=None, ge=1, le=500)
    expected_cols: int | None = Field(default=None, ge=1, le=500)
    expected_control_count: int | None = Field(default=None, ge=1)
    expected_div0_count: int = Field(ge=0)
    expected_total_width_mm: float = Field(gt=0)
    expected_table_height_mm: float = Field(gt=0)
    requested_widths_mm: Annotated[list[Annotated[float, Field(gt=0, le=2000)]], Field(min_length=1, max_length=500)]
    confirm_layout: Literal[True]

    @model_validator(mode='after')
    def widths_match_columns(self):
        if self.expected_cols is not None and len(self.requested_widths_mm) != self.expected_cols:
            raise ValueError('requested_widths_mm must have expected_cols entries')
        return self


# --------------------------------------------------------------------------
# Native table creation from text
# --------------------------------------------------------------------------

CellText = Annotated[str, Field(max_length=2000)]


class _CellValues(Closed):
    rows: int = Field(ge=1, le=200)
    cols: int = Field(ge=1, le=50)
    cells: list[list[CellText]]
    flat_value_hashes: list[Sha64] = Field(description='sha256:<hex> of every cell value, row by row; the backend recomputes them.')
    non_empty_token_count: int = Field(ge=1)
    non_empty_token_preview: Annotated[list[str], Field(max_length=8)]
    warnings: list[Annotated[str, Field(max_length=2000)]] | None = None

    @model_validator(mode='after')
    def cells_match_grid(self):
        if len(self.cells) != self.rows or any(len(row) != self.cols for row in self.cells):
            raise ValueError('cells must have rows lists of cols strings')
        if len(self.flat_value_hashes) != self.rows * self.cols:
            raise ValueError('flat_value_hashes needs one hash per cell')
        return self


class NativeTableInsertStep(_CellValues):
    """Insert a native table at the caret. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['native_table_insert']
    label: Label = 'mcp:native-table-insert'
    field_name: Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_:-]{0,79}$')]
    confirm_native_table: Literal[True]
    source_text_deleted: Literal[False]
    old_plain_text_removal: Literal['deferred_until_rendered_proof']
    next_proof_required: Annotated[str, Field(max_length=2000)] | None = None
    split_by_column: Annotated[str, Field(max_length=500)] | None = None
    split_column_index: int | None = Field(default=None, ge=0)
    split_group_value: Annotated[str, Field(max_length=2000)] | None = None
    split_group_index: int | None = Field(default=None, ge=0)
    split_group_count: int | None = Field(default=None, ge=1)
    split_group_hash: Annotated[str, Field(max_length=100)] | None = None


class AnchorRangeReplaceStep(_CellValues):
    """Replace a proven text range with a native 4-column table. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['anchor_range_replace_native_table']
    label: Label = 'mcp:anchor-range-replace-native-table'
    section_anchor: Text500
    start_anchor: Text500
    end_before_anchor: Text500
    required_source_basename: Annotated[str, Field(min_length=1, max_length=260)] | None = None
    forbid_source_basename: Annotated[str, Field(min_length=1, max_length=260)] | None = None
    expected_range_hash: Sha64 | None = None
    expected_normalized_range_hash: Sha64 | None = None
    caption_text: Text500 | None = None
    cols: Literal[4]
    field_name: Annotated[str, Field(min_length=1, max_length=80, pattern=r'^[^\\/:*?"<>|]+$')]
    confirm_replace: Literal[True]
    next_proof_required: Annotated[str, Field(min_length=1, max_length=2000)]

    @model_validator(mode='after')
    def anchors_differ(self):
        if self.start_anchor.strip() == self.end_before_anchor.strip():
            raise ValueError('start_anchor and end_before_anchor must differ')
        return self


class SelectedTextDeleteStep(Closed):
    """Delete the exactly selected source text. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['selected_text_delete_exact']
    label: Label = 'mcp:selected-text-delete-exact'
    expected_text: Annotated[str, Field(min_length=1, max_length=50000)]
    expected_hash: Sha64 = Field(description='sha256 of expected_text exactly as given.')
    expected_normalized_hash: Sha64 | None = None
    confirm_cleanup: Literal[True]
    source_text_deleted: Literal[False] | None = None
    native_table_proof_ref: Text500 | None = None
    native_table_proof_hash: Text500 | None = None


# --------------------------------------------------------------------------
# Paragraph structure and paragraph options
# --------------------------------------------------------------------------

class _Match(Closed):
    match: Text500
    expected_page: Page


class ParagraphStyleApplyStep(_Match):
    """Set keep-with-next, widow/orphan control and page break before on one paragraph. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['paragraph_style_apply_exact']
    label: Label = 'mcp:paragraph-style-apply-exact'
    keep_with_next: bool | None = None
    widow_orphan: bool | None = None
    pagebreak_before: Literal[0, 1] | None = Field(default=None, description='1 starts the paragraph on a new page.')
    confirm_layout: Literal[True]


class ParagraphDeleteStep(_Match):
    """Delete one paragraph found by text on a page. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['paragraph_delete_exact']
    label: Label = 'mcp:paragraph-delete-exact'
    occurrence_on_page: int | None = Field(default=None, ge=1, le=200)
    expected_previous_contains: Text500 | None = None
    expected_next_contains: Text500 | None = None
    max_page_after: Page | None = None
    confirm_remove: Literal[True]


class ParagraphJoinPreviousStep(_Match):
    """Join one paragraph to the previous one. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['paragraph_join_previous_exact']
    label: Label = 'mcp:paragraph-join-previous-exact'
    expected_previous_contains: Text500 | None = None
    delete_back_count: int | None = Field(default=None, ge=1, le=5)
    max_page_after: Page | None = None
    confirm_layout: Literal[True]


class ParagraphJoinNextStep(_Match):
    """Join the next paragraph into one paragraph. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['paragraph_join_next_exact']
    label: Label = 'mcp:paragraph-join-next-exact'
    expected_next_contains: Text500 | None = None
    delete_count: int | None = Field(default=None, ge=1, le=5)
    next_match: Text500 | None = None
    max_next_page_after: Page | None = None
    insert_line_break: bool | None = None
    move_to_line_end: bool | None = None
    confirm_layout: Literal[True]


Text1000 = Annotated[str, Field(min_length=1, max_length=1000)]


class ParagraphRehomeStep(Closed):
    """Move one paragraph before another (delete and insert). Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['paragraph_rehome_exact']
    label: Label = 'mcp:paragraph-rehome-exact'
    delete_match: Text1000
    delete_expected_page: Page
    insert_before_match: Text1000
    insert_expected_page: Page
    insert_text: Text1000
    expected_text_delta: Literal[0] | None = None
    confirm_layout: Literal[True]


# --------------------------------------------------------------------------
# Text insertion, selection and read-only inspection
# --------------------------------------------------------------------------

AnchorPosition = Literal['before-anchor', 'after-anchor', 'after-paragraph', 'before-heading']


class AnchorInsertStep(Closed):
    """Insert text next to anchor text found in one live operation. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['anchor_insert']
    label: Label = 'mcp:anchor-insert'
    target: Text500 = Field(description='The anchor text, found again in one live operation.')
    position: AnchorPosition | None = Field(default=None, description='Backend default: before-anchor.')
    text: Annotated[str, Field(min_length=1, max_length=10000)] | None = None
    fragments: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode='after')
    def text_or_fragments(self):
        if (self.text is None) == (self.fragments is None):
            raise ValueError('give text or fragments')
        if self.fragments is not None and sum(map(len, self.fragments)) > 10000:
            raise ValueError('fragments are too long')
        return self


class SetTextFileStep(Closed):
    """Insert UNICODE text at the caret through the native text-file insert. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['set_text_file']
    label: Label = 'mcp:set-text-file'
    text: Annotated[str, Field(min_length=1, max_length=10000)]
    format: Literal['UNICODE'] | None = None
    option: Literal['insertfile'] | None = None


class GetSelectedTextStep(Closed):
    """Read the text of the active selection; make one first (select_target). Without an active selection the backend read fails. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['get_selected_text']
    label: Label = 'mcp:get-selected-text'
    keep_select: bool | None = None


class StyleInspectStep(Closed):
    """Read-only style probe at a match. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['style_inspect']
    label: Label = 'mcp:style-inspect'
    match: Text500 | None = None
    keep_position: bool | None = None


class TypographyOverviewStep(Closed):
    """Read-only font, size and style overview of the document. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['typography_overview']
    label: Label = 'mcp:typography-overview'
    scope: Literal['document'] | None = None
    max_samples: int | None = Field(default=None, ge=1, le=200)
    max_sections: int | None = Field(default=None, ge=1, le=200)
    max_styles: int | None = Field(default=None, ge=1, le=200)


class WhereStep(Closed):
    op: Literal['where']
    label: Label = 'mcp:where'


class LayoutInspectStep(Closed):
    op: Literal['layout_inspect']
    label: Label = 'mcp:layout-inspect'


# --------------------------------------------------------------------------
# Page, section and column layout
# --------------------------------------------------------------------------

PAGE_FIELDS = ('paper_width_mm', 'paper_height_mm', 'landscape', 'margin_top_mm', 'margin_bottom_mm', 'margin_left_mm',
               'margin_right_mm', 'header_len_mm', 'footer_len_mm', 'gutter_len_mm')
PaperMm = Annotated[float, Field(ge=10.0, le=1000.0)]
MarginMm = Annotated[float, Field(ge=0.0, le=300.0)]


class LayoutExpectedBefore(Closed):
    """Current values from layout_inspect, for every field the step changes."""
    paper_width_mm: float | None = None
    paper_height_mm: float | None = None
    landscape: bool | None = None
    margin_top_mm: float | None = None
    margin_bottom_mm: float | None = None
    margin_left_mm: float | None = None
    margin_right_mm: float | None = None
    header_len_mm: float | None = None
    footer_len_mm: float | None = None
    gutter_len_mm: float | None = None
    count: int | None = Field(default=None, ge=1)
    gap_mm: float | None = None
    same_width: bool | None = None


_LAYOUT_KIND_FIELDS = {
    'page_setup': {*PAGE_FIELDS, 'apply_to', 'expected_before'},
    'columns': {'count', 'gap_mm', 'same_width', 'apply_to', 'expected_before'},
    'section_insert': set(),
    'section_delete': {'section_index'},
    'hanging_indent': {'marker_text'},
}


class LayoutStep(Closed):
    """Page setup, columns, section break insert/delete or hanging indent at the proven caret (expected_pos from hwpx_where). Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['layout_exact']
    label: Label = 'mcp:layout-exact'
    kind: Literal['page_setup', 'columns', 'section_insert', 'section_delete', 'hanging_indent']
    expected_pos: Pos3
    paper_width_mm: PaperMm | None = None
    paper_height_mm: PaperMm | None = None
    landscape: bool | None = None
    margin_top_mm: MarginMm | None = None
    margin_bottom_mm: MarginMm | None = None
    margin_left_mm: MarginMm | None = None
    margin_right_mm: MarginMm | None = None
    header_len_mm: MarginMm | None = None
    footer_len_mm: MarginMm | None = None
    gutter_len_mm: MarginMm | None = None
    count: int | None = Field(default=None, ge=1, le=10, description='columns: number of columns.')
    gap_mm: float | None = Field(default=None, ge=0.0, le=100.0)
    same_width: bool | None = None
    apply_to: Literal['current_section', 'whole_document', 'from_caret_new'] | None = Field(
        default=None, description='page_setup: current_section or whole_document; columns: current_section or from_caret_new.')
    section_index: int | None = Field(default=None, ge=2, description='section_delete: the section merged into its predecessor.')
    marker_text: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    expected_before: LayoutExpectedBefore | None = None
    confirm_layout: Literal[True]

    @model_validator(mode='after')
    def fields_of_the_kind(self):
        _no_stray(self, {'kind', 'expected_pos', 'confirm_layout', *_LAYOUT_KIND_FIELDS[self.kind]}, f'kind {self.kind}')
        if self.kind == 'page_setup':
            changed = [name for name in PAGE_FIELDS if getattr(self, name) is not None]
            if not changed:
                raise ValueError('page_setup needs at least one page field')
            if self.apply_to not in (None, 'current_section', 'whole_document'):
                raise ValueError('page_setup apply_to is current_section or whole_document')
            before = self.expected_before.model_dump(exclude_none=True) if self.expected_before else None
            if before is None:
                raise ValueError('page_setup requires expected_before from layout_inspect')
            if set(before) - set(PAGE_FIELDS):
                raise ValueError('page_setup expected_before takes page fields only')
            missing = [name for name in changed if name not in before]
            if missing:
                raise ValueError(f'expected_before must include every changed field; missing: {", ".join(missing)}')
        elif self.kind == 'columns':
            if self.count is None:
                raise ValueError('columns requires count')
            if self.gap_mm is not None and (self.count < 2 or self.same_width is False):
                raise ValueError('gap_mm needs count >= 2 and same_width true')
            if self.apply_to not in (None, 'current_section', 'from_caret_new'):
                raise ValueError('columns apply_to is current_section or from_caret_new')
            if self.expected_before is not None:
                before = self.expected_before.model_dump(exclude_none=True)
                if not before or set(before) - {'count', 'gap_mm', 'same_width'}:
                    raise ValueError('columns expected_before takes count, gap_mm and/or same_width')
        elif self.kind == 'section_delete':
            if self.section_index is None:
                raise ValueError('section_delete requires section_index >= 2')
            if self.expected_pos[2] != 0:
                raise ValueError('section_delete expected_pos must be at pos 0')
        elif self.kind == 'hanging_indent':
            if self.marker_text is None or not self.marker_text.strip() or any(c in self.marker_text for c in '\r\n\t'):
                raise ValueError('hanging_indent requires visible marker_text without line breaks or tabs')
        return self


# --------------------------------------------------------------------------
# Notes, fields, bookmarks, equations, shapes, headers and footers
# --------------------------------------------------------------------------

_OBJECT_KIND_FIELDS = {
    'footnote': {'text'}, 'endnote': {'text'}, 'memo': {'text'},
    'hyperlink': {'url', 'display_text'}, 'bookmark': {'name'}, 'equation': {'script'},
    'line': {'width_mm', 'height_mm', 'treat_as_char'}, 'rectangle': {'width_mm', 'height_mm', 'treat_as_char'},
    'ellipse': {'width_mm', 'height_mm', 'treat_as_char'},
    'header': {'text', 'apply_to'}, 'footer': {'text', 'apply_to'},
}
ShapeMm = Annotated[float, Field(ge=0.0, le=1000.0)]


class ObjectInsertStep(Closed):
    """Insert one note, field, bookmark, equation, shape, header or footer at the proven caret (expected_pos from hwpx_where). Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['object_insert_exact']
    label: Label = 'mcp:object-insert-exact'
    kind: Literal['footnote', 'endnote', 'memo', 'hyperlink', 'bookmark', 'equation', 'line', 'rectangle', 'ellipse',
                  'header', 'footer']
    expected_pos: Pos3
    confirm_mutation: Literal[True]
    text: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    url: Annotated[str, Field(min_length=1, max_length=2000)] | None = Field(default=None, description='http, https or mailto.')
    display_text: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    script: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    width_mm: ShapeMm | None = None
    height_mm: ShapeMm | None = None
    treat_as_char: bool | None = None
    apply_to: Literal['both', 'even', 'odd'] | None = None

    @model_validator(mode='after')
    def fields_of_the_kind(self):
        allowed = _OBJECT_KIND_FIELDS[self.kind]
        _no_stray(self, {'kind', 'expected_pos', 'confirm_mutation', *allowed}, f'kind {self.kind}')
        required = allowed - {'treat_as_char', 'apply_to'}
        missing = sorted(name for name in required if getattr(self, name) is None)
        if missing:
            raise ValueError(f'kind {self.kind} requires: {", ".join(missing)}')
        for name in ('text', 'display_text', 'name', 'url'):
            value = getattr(self, name)
            if value is not None and (not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError(f'{name} must be visible text without control characters')
        if self.script is not None and (not self.script.strip() or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127 for c in self.script)):
            raise ValueError('script must not contain control characters other than line breaks and tabs')
        if self.url is not None:
            if any(c.isspace() for c in self.url) or ';' in self.url or '|' in self.url:
                raise ValueError('url must not contain whitespace, ";" or "|"')
            parts = urlsplit(self.url)
            if parts.scheme.lower() not in {'http', 'https', 'mailto'} or (parts.scheme.lower() != 'mailto' and not parts.netloc) \
                    or (parts.scheme.lower() == 'mailto' and not parts.path):
                raise ValueError('url must be http(s) with a host, or mailto with an address')
        if self.kind == 'line' and self.width_mm == 0 and self.height_mm == 0:
            raise ValueError('a line needs a non-zero width_mm or height_mm')
        if self.kind in {'rectangle', 'ellipse'} and (self.width_mm == 0 or self.height_mm == 0):
            raise ValueError(f'{self.kind} needs positive width_mm and height_mm')
        return self


# --------------------------------------------------------------------------
# Low-level actions: only the backend's exact bundle allowlists
# --------------------------------------------------------------------------

SafeAction = Literal['Cancel', 'TableCellBlock', 'TableLeftCell', 'TableRightCell', 'TableUpperCell', 'TableLowerCell',
                     'MoveDocBegin', 'MoveDocEnd', 'MoveLeft', 'MoveRight', 'MoveUp', 'MoveDown']
SafePyhwpx = Literal['CurFieldState', 'get_pos', 'GetPos', 'get_selected_pos', 'GetSelectedPos', 'get_selected_text',
                     'GetSelectedText', 'get_text_file', 'GetTextFile', 'MoveDocBegin', 'MoveDocEnd', 'MoveLeft',
                     'MoveRight', 'MoveUp', 'MoveDown']


class HwpActionStep(Closed):
    """One allowlisted navigation or selection action. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['hwp_action']
    label: Label = 'mcp:hwp-action'
    action_name: SafeAction = Field(description='Navigation or selection only; the backend command-bundle allowlist.')


def _json_ok(value: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if value is None or isinstance(value, (bool, int, str)):
        return not isinstance(value, str) or len(value) <= 10000
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return len(value) <= 20 and all(_json_ok(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 50 and all(isinstance(k, str) and k and not k.startswith('_') and '__' not in k
                                        and _json_ok(v, depth + 1) for k, v in value.items())
    return False


class PyhwpxCallStep(Closed):
    """One allowlisted pyhwpx read or caret call. Not one of the backend's guarded exact engines: any backend error in this step, including a stale or wrong target, is reported as an unknown outcome and holds the session."""
    op: Literal['pyhwpx_call']
    label: Label = 'mcp:pyhwpx-call'
    method_path: SafePyhwpx = Field(description='Position, selection and text reads or caret moves; the backend command-bundle allowlist.')
    args: Annotated[list[Any], Field(max_length=20)] | None = None
    kwargs: dict[str, Any] | None = None

    @model_validator(mode='after')
    def json_arguments(self):
        if not _json_ok(self.args or []) or not _json_ok(self.kwargs or {}):
            raise ValueError('args and kwargs must be bounded JSON without private keys')
        return self


# --------------------------------------------------------------------------
# Direct live-session routes (not command bundles)
# --------------------------------------------------------------------------

Direction = Literal['left', 'right', 'up', 'down']


class TargetInfoOp(Closed):
    op: Literal['target_info']
    target: Text500 = Field(description='Text to find, or a match number from the last hwpx_find.')


class MoveToTargetOp(Closed):
    op: Literal['move_to_target']
    target: Text500


class SelectTargetOp(Closed):
    op: Literal['select_target']
    target: Text500


class CellInfoOp(Closed):
    op: Literal['cell_info']


class CellMoveOp(Closed):
    op: Literal['cell_move']
    direction: Direction
    count: int = Field(ge=1, le=500)


class CursorMoveOp(Closed):
    op: Literal['cursor_move']
    direction: Direction
    count: int = Field(ge=1, le=500)


class TypeTextOp(Closed):
    op: Literal['type_text']
    text: Annotated[str, Field(min_length=1, max_length=10000)]
    allow_insert_at_caret: bool = Field(description='Typing at a bare caret without a selection must be requested explicitly.')


class FigureSectionOp(Closed):
    op: Literal['figure_section']
    target_heading: Text500
    heading: Text500
    intro: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    caption: Text500 | None = None
    body: Annotated[str, Field(min_length=1, max_length=4000)] | None = None


class CellReplaceOp(Closed):
    op: Literal['cell_replace']
    anchor: Text500 | None = Field(default=None, description='A sentence near the table.')
    cell: CellAddr | None = None
    text: Annotated[str, Field(min_length=1, max_length=20000)]
    expect_cell: CellAddr | None = None
    expect_old: Annotated[str, Field(min_length=1, max_length=20000)] | None = None
    expect_new: Annotated[str, Field(min_length=1, max_length=20000)] | None = None
    expected_page: Page | None = None

    @model_validator(mode='after')
    def anchor_or_cell(self):
        if self.anchor is None and self.cell is None:
            raise ValueError('give anchor or cell')
        return self


class FontSizeOp(Closed):
    op: Literal['font_size']
    size_pt: float = Field(gt=0, le=4096)
    at_caret: bool


class BoldOp(Closed):
    op: Literal['bold']
    enabled: bool


class FontFamilyOp(Closed):
    op: Literal['font_family']
    face_name: Annotated[str, Field(min_length=1, max_length=64)]
    at_caret: bool


class BulletOp(Closed):
    op: Literal['bullet']
    text: Annotated[str, Field(min_length=1, max_length=10000)]


class TableInsertOp(Closed):
    op: Literal['table_insert']
    rows: int = Field(ge=1, le=500)
    cols: int = Field(ge=1, le=100)


class ListItemsOp(Closed):
    op: Literal['list_items']
    count: int = Field(ge=1, le=100)


class NumberingReadbackOp(Closed):
    op: Literal['numbering_readback']


REST_OPS = {
    'target_info': ('/local-cli/info', TargetInfoOp), 'move_to_target': ('/local-cli/move', MoveToTargetOp),
    'select_target': ('/local-cli/select', SelectTargetOp), 'cell_info': ('/local-cli/cell', CellInfoOp),
    'cell_move': ('/local-cli/cellmove', CellMoveOp), 'cursor_move': ('/local-cli/cursormove', CursorMoveOp),
    'type_text': ('/local-cli/type', TypeTextOp), 'figure_section': ('/local-cli/figure-section', FigureSectionOp),
    'cell_replace': ('/local-cli/cell-replace', CellReplaceOp), 'font_size': ('/local-cli/fontsize', FontSizeOp),
    'bold': ('/local-cli/bold', BoldOp), 'font_family': ('/local-cli/font', FontFamilyOp),
    'bullet': ('/local-cli/bullet', BulletOp), 'table_insert': ('/local-cli/table', TableInsertOp),
    'list_items': ('/local-cli/list', ListItemsOp), 'numbering_readback': ('/local-cli/numbering-readback', NumberingReadbackOp),
}

NEW_BUNDLE_STEPS = (
    ControlInventoryStep, TableFrameInventoryStep, ExactControlSelectProofStep, TableCellStructureStep, ControlDeleteStep,
    ControlMoveResizeStep, ControlJoinPreviousStep, CellRowFitStep, TableSplitStep, TableStructureStep, TableColumnWidthStep,
    NativeTableInsertStep, AnchorRangeReplaceStep, SelectedTextDeleteStep, ParagraphStyleApplyStep, ParagraphDeleteStep,
    ParagraphJoinPreviousStep, ParagraphJoinNextStep, ParagraphRehomeStep, AnchorInsertStep, SetTextFileStep,
    GetSelectedTextStep, StyleInspectStep, TypographyOverviewStep, WhereStep, LayoutStep, LayoutInspectStep,
    ObjectInsertStep, HwpActionStep, PyhwpxCallStep,
)
