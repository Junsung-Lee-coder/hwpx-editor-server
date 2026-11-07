"""The request rules of the named-style commands, shared by the backend validators and the MCP models.

Pure and standard-library only, so the MCP adapter can import it without loading any native engine.
"""
from __future__ import annotations

import math
import re
from typing import Any

NAME_MAX_CHARS = 64
KINDS = ('paragraph', 'character')


def name_problem(value: Any) -> str | None:
    """Why this is not a style name a request may give, or None: 1 to 64 characters, no control character, no unpaired
    surrogate and no space at either end."""
    if (type(value) is not str or not value or len(value) > NAME_MAX_CHARS or value != value.strip()
            or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        return (f'a style name has 1 to {NAME_MAX_CHARS} characters, without control characters, unpaired surrogates '
                'or spaces at either end')
    return None


# Fixed labels a held style edit may carry into the public failure_diagnostic: where it stopped, and the builtin shape of what
# the native call answered there. Never a value, a message, a type name or anything the answer defines. The edit's caret steps,
# the legacy action family of the create and change commands, and the document text read.
_LANGUAGES = ('Hangul', 'Latin', 'Hanja', 'Japanese', 'Other', 'Symbol', 'User')
_SUBSET_ITEMS = ('Height', 'TextColor', 'Bold', 'Italic', 'UnderlineType', *(f'FaceName{name}' for name in _LANGUAGES),
                 *(f'FontType{name}' for name in _LANGUAGES))
NATIVE_SITES = frozenset({
    'document.text', 'caret.method', 'caret.place', 'caret.read',
    'create_action.member', 'create_action.call', 'action.member', 'create_set.call', 'set.member', 'get_default.call',
    'set_item.NameLocal', 'set_item.NameEng', 'set_item.Next', 'set_item.CharShape',
    'item_exist.CharShape', 'item.CharShape', 'subset.member',
    'char_shape.read', 'char_shape.member', 'char_shape.item_exist', 'char_shape.item',
    *(f'subset_set_item.{item}' for item in _SUBSET_ITEMS),
})
NATIVE_RETURNS = frozenset({'none', 'true', 'false', 'int', 'float', 'complex', 'str', 'bytes', 'tuple', 'list', 'dict', 'set', 'object',
                            'not_callable', 'raised', 'absent', 'not_canonical', 'empty', 'not_well_formed'})


# The bounded facts of a held style edit's proof (native_styles._not_as_requested): which predicate failed for which style, field or
# place, measured before the edit is held, so a caller and the journal keep them. Style names (at most NAME_MAX_CHARS), fixed labels,
# indexes and counts; never document text, a value, a path or an exception.
STYLE_PROOF_SCHEMA = 'style-proof/v1'
STYLE_PROOF_SHOWN = 20
STYLE_PROOF_COMMANDS = frozenset({'style_apply_exact', 'style_create_exact', 'style_update_exact'})
STYLE_PROOF_PREDICATES = frozenset({
    'document_changed', 'old_style_missing', 'old_style_duplicated', 'old_style_changed', 'next_style_dangling', 'style_added',
    'new_style_missing', 'new_style_duplicated', 'new_style_wrong', 'changed_style_wrong', 'paragraph_style', 'paragraph_changed',
    'paragraph_moved', 'target_paragraph',
})
STYLE_PROOF_FIELDS = frozenset({'settings', 'character', 'paragraph', 'next', 'kind', 'english_name', 'lock', 'language', 'fonts'})
# What of a character shape differs from the one expected (native_styles._character_facts): the size expected and found in points, the
# differing items by fixed label (at most CHARACTER_ITEMS_SHOWN) and how many there are, and whether any of them was not requested.
CHARACTER_FACT_KEYS = ('size_expected', 'size_found', 'character_items', 'character_item_count', 'unrequested_changed')
CHARACTER_ITEMS_SHOWN = 32
SIZE_MAX_PT = 10_000.0
_PROOF_ORDER = ('schema', 'command', 'old_styles', 'new_styles', 'matched', 'added', 'problem_count', 'problems', 'problems_cut')
_PROOF_KEYS = frozenset(_PROOF_ORDER)
_PROBLEM_ORDER = ('predicate', 'name', 'old_index', 'new_index', 'fields', 'attributes', 'expected', 'found', 'where', 'field', *CHARACTER_FACT_KEYS)
_WHERE = re.compile(r'(?:paragraph [0-9]{1,7}|(?:(?:header|footer):s[0-9]{1,4}:[a-z]{0,8}|header|footer|other|body|table_cell) paragraph [0-9]{1,7})')
_TOKEN = re.compile(r'[a-z0-9_]{1,40}')
_COUNT_MAX = 1_000_000


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= _COUNT_MAX


def _size(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= SIZE_MAX_PT


def _label(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= NAME_MAX_CHARS and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _proof_problem(item: Any) -> dict[str, Any] | None:
    if type(item) is not dict or type(item.get('predicate')) is not str or item['predicate'] not in STYLE_PROOF_PREDICATES:
        return None
    checks = {
        'predicate': lambda value: True, 'name': _label, 'expected': _label, 'found': _label, 'old_index': _count, 'new_index': _count,
        'where': lambda value: type(value) is str and _WHERE.fullmatch(value) is not None,
        'field': lambda value: type(value) is str and _TOKEN.fullmatch(value) is not None,
        'fields': lambda value: (type(value) is list and 0 < len(value) <= len(STYLE_PROOF_FIELDS)
                                 and all(type(field) is str and field in STYLE_PROOF_FIELDS for field in value) and len(set(value)) == len(value)),
        'attributes': lambda value: (type(value) is list and 0 < len(value) <= 16
                                     and all(type(key) is str and _TOKEN.fullmatch(key) is not None for key in value)),
        'size_expected': _size, 'size_found': _size,
        'character_items': lambda value: (type(value) is list and 0 < len(value) <= CHARACTER_ITEMS_SHOWN
                                          and all(type(key) is str and _TOKEN.fullmatch(key) is not None for key in value) and len(set(value)) == len(value)),
        'character_item_count': _count, 'unrequested_changed': lambda value: type(value) is bool,
    }
    for key in item:
        if type(key) is not str or key not in checks or not checks[key](item[key]):
            return None
    if 'character_items' in item and item.get('character_item_count', -1) < len(item['character_items']):
        return None
    return {key: list(item[key]) if type(item[key]) is list else item[key] for key in _PROBLEM_ORDER if key in item}


def style_proof_facts(value: Any) -> dict[str, Any] | None:
    """A fresh copy of the facts when they are exactly of their fixed shape, else None. Reads plain builtin values only."""
    if type(value) is not dict or set(value) != _PROOF_KEYS:
        return None
    if value['schema'] != STYLE_PROOF_SCHEMA or type(value['command']) is not str or value['command'] not in STYLE_PROOF_COMMANDS:
        return None
    if not all(_count(value[key]) for key in ('old_styles', 'new_styles', 'matched', 'problem_count')):
        return None
    added = value['added']
    if added is not None and not (type(added) is dict and set(added) == {'name', 'index'} and _label(added['name']) and _count(added['index'])):
        return None
    problems = value['problems']
    if type(problems) is not list or len(problems) > STYLE_PROOF_SHOWN or type(value['problems_cut']) is not bool:
        return None
    if value['problem_count'] < len(problems) or value['problems_cut'] != (value['problem_count'] > len(problems)):
        return None
    copied = [_proof_problem(item) for item in problems]
    if any(item is None for item in copied):
        return None
    fresh = {'added': {'name': added['name'], 'index': added['index']} if added is not None else None, 'problems': copied}
    return {key: fresh[key] if key in fresh else value[key] for key in _PROOF_ORDER}
