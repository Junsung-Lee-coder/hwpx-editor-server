"""Closed adapter schemas; field spellings follow the r16 REST and bundle boundaries."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import CellMarginsGetRequest

SessionId = Annotated[str, Field(pattern=r'^[a-f0-9]{32}$')]
Text = Annotated[str, Field(min_length=1, max_length=16384)]
Label = Annotated[str, Field(min_length=1, max_length=80)]


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Health(Closed):
    pass


class Session(Closed):
    session_id: SessionId


class OpenRequest(Closed):
    source_path: Annotated[str, Field(min_length=1, max_length=4096)]
    session_label: Annotated[str, Field(max_length=128)] | None = None


class Open(Closed):
    request: OpenRequest


class FindRequest(Closed):
    query: Text
    around: int = Field(default=0, ge=0, le=5)
    with_page: bool = False
    proof_match: int | None = Field(default=None, ge=1, le=10000)


class Find(Session):
    request: FindRequest


class ContextStep(Closed):
    op: Literal['context']
    label: Label = 'mcp:context'


class SelectionStep(Closed):
    op: Literal['selection_proof']
    label: Label = 'mcp:selection-proof'


class ReadbackStep(Closed):
    op: Literal['readback']
    label: Label = 'mcp:readback'
    scope: Literal['document', 'selection'] = 'document'
    page_from: int | None = Field(default=None, ge=1, le=10000)
    page_to: int | None = Field(default=None, ge=1, le=10000)
    max_blocks: int = Field(default=100, ge=1, le=1000)
    max_table_cells: int = Field(default=100, ge=1, le=1000)
    max_controls: int = Field(default=100, ge=1, le=1000)


class CellFormatStep(Closed):
    # Deliberately a subset of cell_format_exact, not a generic COM/action escape.
    op: Literal['cell_format_exact']
    label: Label = 'mcp:cell-format-exact'
    section_anchor: Text
    target_id: Text
    # Native control inventory proofs are intentionally truncated to 24 hex
    # characters; retain acceptance of the full-width form for callers that
    # provide an independently computed proof.
    expected_hash: Annotated[str, Field(pattern=r'^(sha256:)?(?:[a-f0-9]{24}|[a-f0-9]{64})$')]
    expected_page: int = Field(ge=1, le=10000)
    page_from: int = Field(ge=1, le=10000)
    page_to: int = Field(ge=1, le=10000)
    around: int = Field(default=0, ge=0, le=5)
    vertical_align: Literal['top', 'center', 'bottom'] | None = None
    cell_margin_hu: int | None = Field(default=None, ge=1, le=100000)
    cell_margin_mm: float | None = Field(default=None, gt=0.0, le=70.0)
    confirm_layout: Literal[True]
    max_controls: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode='after')
    def require_one_format_selector(self) -> 'CellFormatStep':
        selectors = (self.vertical_align, self.cell_margin_hu, self.cell_margin_mm)
        if sum(value is not None for value in selectors) != 1:
            raise ValueError('cell_format_exact requires exactly one format selector')
        return self


class PageNumberStep(Closed):
    # Prefix/suffix text is unsupported by the native action, so it has no field.
    op: Literal['page_number_apply']
    label: Label = 'mcp:page-number-apply'
    position: Literal['top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right']
    start_number: int = Field(default=1, ge=1, le=65535)
    side_char: Literal['none', 'dash'] = 'none'
    confirm_layout: bool

    @field_validator('confirm_layout')
    @classmethod
    def require_confirm_layout(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError('confirm_layout must be true')
        return value


class OfficialNumberingStep(Closed):
    op: Literal['official_numbering_apply']
    label: Label = 'mcp:official-numbering-apply'
    match: Annotated[str, Field(min_length=1, max_length=500)]
    expected_page: int = Field(ge=1, le=10000)
    level: int = Field(ge=1, le=4)
    confirm_layout: bool

    @field_validator('confirm_layout')
    @classmethod
    def require_confirm_layout(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError('confirm_layout must be true')
        return value

    @model_validator(mode='after')
    def require_single_line_match(self) -> 'OfficialNumberingStep':
        if not self.match.strip() or '\n' in self.match or '\r' in self.match:
            raise ValueError('official_numbering_apply match must be a non-blank single line')
        return self


class _ParagraphTarget(Closed):
    match: Annotated[str, Field(min_length=1, max_length=500)]
    expected_page: int = Field(ge=1, le=10000)

    @model_validator(mode='after')
    def require_single_line_match(self):
        if not self.match.strip() or '\n' in self.match or '\r' in self.match:
            raise ValueError('match must be a non-blank single line')
        return self


class ParagraphInsertStep(_ParagraphTarget):
    op: Literal['paragraph_insert_exact']
    label: Label = 'mcp:paragraph-insert-exact'
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    position: Literal['after', 'before'] = 'after'
    confirm_layout: Literal[True]


class ParagraphRemoveStep(_ParagraphTarget):
    op: Literal['paragraph_remove_exact']
    label: Label = 'mcp:paragraph-remove-exact'
    expected_text: Annotated[str, Field(min_length=1, max_length=4000)]
    confirm_remove: Literal[True]


def _in_tenths(value: float | None) -> bool:
    return value is None or abs(value * 10 - round(value * 10)) <= 1e-9


class _FormatTarget(Closed):
    """An exact range: unique text, optionally through a second unique text, or one paragraph by its review index and full text."""
    match: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    through_match: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    paragraph_index: int | None = Field(default=None, ge=0)
    expected_text: Annotated[str, Field(max_length=4000)] | None = None
    expected_page: int = Field(ge=1, le=10000)
    confirm_layout: Literal[True]

    @model_validator(mode='after')
    def require_one_target(self):
        if self.match is not None:
            if self.paragraph_index is not None or self.expected_text is not None:
                raise ValueError('give match or paragraph_index with expected_text, not both')
            for value in (self.match, self.through_match):
                if value is not None and (not value.strip(' \t\n\u00a0\u2007') or any(ord(char) < 32 and char not in '\t\n' for char in value)):
                    raise ValueError('match must be text without control characters other than tab and line break')
        elif self.through_match is not None or self.paragraph_index is None or self.expected_text is None:
            raise ValueError('give match, or paragraph_index with expected_text')
        return self


class CharacterFormatStep(_FormatTarget):
    op: Literal['character_format_exact']
    label: Label = 'mcp:character-format-exact'
    font_family: Annotated[str, Field(min_length=1, max_length=64, pattern=r'^[^@\s](.*\S)?$')] | None = None
    size_pt: float | None = Field(default=None, ge=0.1, le=4096.0, description='Points, in steps of 0.1.')
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    text_color: Annotated[str, Field(pattern=r'^#[0-9A-Fa-f]{6}$')] | None = Field(default=None, description='#RRGGBB.')

    @model_validator(mode='after')
    def require_a_property(self):
        if all(value is None for value in (self.font_family, self.size_pt, self.bold, self.italic, self.underline, self.text_color)):
            raise ValueError('character_format_exact needs at least one property')
        if not _in_tenths(self.size_pt):
            raise ValueError('size_pt goes in steps of 0.1')
        return self


class ParagraphFormatStep(_FormatTarget):
    op: Literal['paragraph_format_exact']
    label: Label = 'mcp:paragraph-format-exact'
    align: Literal['left', 'center', 'right', 'justify'] | None = None
    left_indent_pt: float | None = Field(default=None, ge=0.0, le=570.2, description='Left margin in points, in steps of 0.1.')
    right_indent_pt: float | None = Field(default=None, ge=0.0, le=570.2, description='Right margin in points, in steps of 0.1.')
    first_line_indent_pt: float | None = Field(default=None, ge=-570.2, le=570.2, description='First line in points, in steps of 0.1: positive indents it, negative hangs it.')
    line_spacing_type: Literal['percent', 'fixed', 'between_lines', 'at_least'] | None = None
    line_spacing_value: float | None = Field(default=None, ge=0.0, le=841.8, description='A whole percent 1..500 for percent; otherwise points in steps of 0.1.')

    @model_validator(mode='after')
    def require_a_property(self):
        if all(value is None for value in (self.align, self.left_indent_pt, self.right_indent_pt, self.first_line_indent_pt, self.line_spacing_type)):
            raise ValueError('paragraph_format_exact needs at least one property')
        if (self.line_spacing_type is None) != (self.line_spacing_value is None):
            raise ValueError('line_spacing_type and line_spacing_value go together')
        if not all(_in_tenths(value) for value in (self.left_indent_pt, self.right_indent_pt, self.first_line_indent_pt)):
            raise ValueError('indents go in steps of 0.1 points')
        if self.line_spacing_type == 'percent':
            if self.line_spacing_value != int(self.line_spacing_value) or not 1 <= self.line_spacing_value <= 500:
                raise ValueError('a percent line spacing is a whole number from 1 to 500')
        elif self.line_spacing_type is not None and (not _in_tenths(self.line_spacing_value) or (self.line_spacing_type != 'between_lines' and self.line_spacing_value == 0)):
            raise ValueError('this line spacing is in points, in steps of 0.1')
        return self


class UndoStep(Closed):
    op: Literal['undo']


class RedoStep(Closed):
    op: Literal['redo']


class Reconcile(Closed):
    op: Literal['command_reconcile']
    command_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.:-]+$')]


class Command(Session):
    request: Annotated[ContextStep | SelectionStep | ReadbackStep | CellFormatStep | PageNumberStep | OfficialNumberingStep | ParagraphInsertStep | ParagraphRemoveStep | CharacterFormatStep | ParagraphFormatStep | UndoStep | RedoStep | Reconcile,
                       Field(discriminator='op')]


class FrameProof(Closed):
    kind: Literal['frame']


class PageProof(Closed):
    kind: Literal['page']
    page: int = Field(ge=1, le=10000)
    dpi: int = Field(default=160, ge=72, le=600)


class ReviewProof(Closed):
    kind: Literal['review']
    dpi: int = Field(default=100, ge=72, le=300)
    max_pages: int = Field(default=30, ge=1, le=200)
    inline_images: int = Field(default=3, ge=0, le=8)
    compare_to: Annotated[str, Field(pattern=r'^[a-f0-9]{32}/review-[0-9]{1,6}$')] | None = None


class Proof(Session):
    request: Annotated[FrameProof | PageProof | ReviewProof, Field(discriminator='kind')]


class Error(Closed):
    code: str
    message: str
    details: dict[str, Any]


class Envelope(Closed):
    ok: bool
    operation: str
    session_id: str | None
    document_id: str | None
    result: dict[str, Any] | None
    error: Error | None


MODELS = {
    'hwpx_health': Health, 'hwpx_open': Open, 'hwpx_status': Session,
    'hwpx_find': Find, 'hwpx_where': Session, 'hwpx_command': Command,
    'hwpx_proof': Proof, 'hwpx_save': Session, 'hwpx_close': Session,
    'hwpx_cell_margins_get': CellMarginsGetRequest,
}
