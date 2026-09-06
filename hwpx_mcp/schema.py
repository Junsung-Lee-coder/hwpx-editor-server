"""Closed adapter schemas; field spellings follow the r16 REST and bundle boundaries."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field

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
    vertical_align: Literal['top', 'center', 'bottom']
    confirm_layout: Literal[True]
    max_controls: int = Field(default=100, ge=1, le=1000)


class Reconcile(Closed):
    op: Literal['command_reconcile']
    command_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.:-]+$')]


class Command(Session):
    request: Annotated[ContextStep | SelectionStep | ReadbackStep | CellFormatStep | Reconcile,
                       Field(discriminator='op')]


class FrameProof(Closed):
    kind: Literal['frame']


class PageProof(Closed):
    kind: Literal['page']
    page: int = Field(ge=1, le=10000)
    dpi: int = Field(default=160, ge=72, le=600)


class Proof(Session):
    request: Annotated[FrameProof | PageProof, Field(discriminator='kind')]


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
}
