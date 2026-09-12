"""Shared strict models and canonical request hash for the targeted four-margin getter.

These models define the closed public request surface shared by the REST route
and the MCP adapter. They depend only on the standard library and Pydantic;
native COM, FastAPI and the optional MCP SDK are intentionally not imported.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SESSION_ID_PATTERN = r'^[a-f0-9]{32}$'
_TARGET_ID_PATTERN = r'^ctrl/[0-9]+/tbl/[^/]+$'
_GENERATION_SID_PATTERN = r'^local-cli/live-document/v1:(?P<sid>[a-f0-9]{32}):sha256:(?P<digest>[a-f0-9]{64})$'
# The native control-inventory proof is a truncated SHA-256 (24 lowercase hex
# characters) with an optional ``sha256:`` prefix.  The setter's wider legacy
# forms are deliberately not accepted by this getter.
_EXPECTED_HASH_PATTERN = r'^(?:sha256:)?[a-f0-9]{24}$'

_HWPUNIT_INT_MAX = 2147483647


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class CellMarginsGetTarget(_ClosedModel):
    """Exact live target binding for one four-side cell-margin observation."""

    document_id: Annotated[str, Field(pattern=_SESSION_ID_PATTERN)]
    expected_document_generation: Annotated[str, Field(pattern=_GENERATION_SID_PATTERN)]
    target_id: Annotated[str, Field(min_length=1, max_length=500, pattern=_TARGET_ID_PATTERN)]
    expected_hash: Annotated[str, Field(pattern=_EXPECTED_HASH_PATTERN)]
    expected_page: int = Field(ge=1, le=10000)
    expected_cell_page: int = Field(ge=1, le=10000)
    page_from: int = Field(ge=1, le=10000)
    page_to: int = Field(ge=1, le=10000)
    cell_pos: Annotated[list[int], Field(min_length=3, max_length=3)]
    cell_addr: Annotated[list[int], Field(min_length=2, max_length=2)]
    section_anchor: Annotated[str, Field(min_length=1, max_length=1024)]
    max_controls: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode='after')
    def bind_exact_live_target(self) -> 'CellMarginsGetTarget':
        for name in ('cell_pos', 'cell_addr'):
            values = getattr(self, name)
            if any(isinstance(item, bool) or item < 0 or item > _HWPUNIT_INT_MAX for item in values):
                raise ValueError(f'{name} entries must be nonnegative integers <= {_HWPUNIT_INT_MAX}')
        if self.page_from > self.page_to:
            raise ValueError('page_from must not exceed page_to')
        if not (self.page_from <= self.expected_page <= self.page_to):
            raise ValueError('expected_page must lie within the closed page range')
        if not (self.page_from <= self.expected_cell_page <= self.page_to):
            raise ValueError('expected_cell_page must lie within the closed page range')
        generation_parts = self.expected_document_generation.split(':')
        if len(generation_parts) != 4 or generation_parts[1] != self.document_id:
            raise ValueError('expected_document_generation must carry the same session id as document_id')
        if self.target_id != self.target_id.strip():
            raise ValueError('target_id must not have surrounding whitespace')
        if self.section_anchor != self.section_anchor.strip():
            raise ValueError('section_anchor must not have leading or trailing whitespace')
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in self.section_anchor):
            raise ValueError('section_anchor must not contain control characters')
        return self

    def normalized_expected_hash(self) -> str:
        raw = self.expected_hash
        return raw if raw.startswith('sha256:') else 'sha256:' + raw


class CellMarginsGetRequest(_ClosedModel):
    """Outer REST/MCP request: one explicit session plus one exact target."""

    session_id: Annotated[str, Field(pattern=_SESSION_ID_PATTERN)]
    request: CellMarginsGetTarget

    @model_validator(mode='after')
    def bind_session_identity(self) -> 'CellMarginsGetRequest':
        if self.request.document_id != self.session_id:
            raise ValueError('request.document_id must equal the outer session_id')
        return self


def canonical_cell_margins_request_sha256(request: CellMarginsGetRequest) -> str:
    """Compute the canonical full-request digest shared by REST and MCP.

    The model is re-validated first so callers always hash the normalized
    shape (defaulted ``max_controls``, prefixed ``expected_hash``).
    """

    validated = CellMarginsGetRequest.model_validate(request.model_dump(mode='json'))
    payload = {
        'operation': 'cell_margins_get',
        'session_id': validated.session_id,
        'request': validated.request.model_dump(mode='json'),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return 'sha256:' + hashlib.sha256(encoded).hexdigest()
