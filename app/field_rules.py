"""The request rules of the named-field commands, shared by the backend validators and the MCP models.

Pure and standard-library only, so the MCP adapter can import it without loading any native engine.
"""
from __future__ import annotations

from typing import Any

NAME_MAX_CHARS = 200
TEXT_MAX_CHARS = 2000
LINE_BREAKS = ('\r', '\n')


def _unpaired_surrogate(value: str) -> bool:
    # A lone UTF-16 surrogate code point; a character outside the BMP is one code point and is not one.
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def name_problem(value: Any) -> str | None:
    """Why this is not a field name a request may give, or None: 1 to 200 characters, no control character, no unpaired surrogate, no {{ or }}."""
    if (type(value) is not str or not value or len(value) > NAME_MAX_CHARS or any(ord(char) < 32 for char in value)
            or _unpaired_surrogate(value) or '{{' in value or '}}' in value):
        return (f'the name must be the exact field name field_inventory lists, 1 to {NAME_MAX_CHARS} characters, '
                'without control characters, unpaired surrogates or {{ }} (the official occurrence syntax)')
    return None


def text_problem(value: Any) -> str | None:
    """Why this is not a new_text, or None: at most 2000 characters on one line, tabs allowed, no other control character or unpaired surrogate."""
    if type(value) is not str or len(value) > TEXT_MAX_CHARS:
        return f'new_text must be text of at most {TEXT_MAX_CHARS} characters'
    if any(char in LINE_BREAKS for char in value):
        return 'new_text goes into the field on one line: a paragraph break inside a field is not written by this command'
    if any(ord(char) < 32 and char != '\t' for char in value) or _unpaired_surrogate(value):
        return 'new_text may hold tabs but no other control characters or unpaired surrogates'
    return None
