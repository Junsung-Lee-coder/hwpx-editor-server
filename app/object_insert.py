"""Pure planning and verification for exact object insertion at the caret.

Nothing here touches Hancom. The service mixin reads the whole document as
HWPML (``GetTextFile('HWPML2X', '')``) before and after one native insert,
and these helpers decide whether the observed document proves that exactly
one expected object was added and nothing else changed. The proof is
positional (see ``evaluate_insert``): the requested properties are checked on
the control found at the insertion point, never on any matching control.

The HWPML element names below are HWPML 2.x names taken from the task
specification; they are **not natively verified** yet. Every name the
verifier relies on lives in ``KIND_TAGS`` / ``CONTROL_TAGS`` /
``TEXT_TAGS`` / ``ATTRS`` so it can be corrected in one place after a native
run. A wrong name makes verification fail closed (the edit is reported as
possibly persisted, never as succeeded).
"""

from __future__ import annotations

import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.hwpml_invariants import attrs, head_tail_reasons, signature

OP = 'object_insert_exact'
SCHEMA_VERSION = 'local-cli/object-insert-exact/v1'

# kind -> (HWPML tag, FIELDBEGIN Type or None). Correct here after native testing.
KIND_TAGS: dict[str, tuple[str, str | None]] = {
    'footnote': ('FOOTNOTE', None),
    'endnote': ('ENDNOTE', None),
    'memo': ('FIELDBEGIN', 'Memo'),
    'hyperlink': ('FIELDBEGIN', 'Hyperlink'),
    'bookmark': ('BOOKMARK', None),
    'equation': ('EQUATION', None),
    'line': ('LINE', None),
    'rectangle': ('RECTANGLE', None),
    'ellipse': ('ELLIPSE', None),
    'header': ('HEADER', None),
    'footer': ('FOOTER', None),
}

# Controls a field insert adds next to its FIELDBEGIN (counted by tag only).
FIELD_COMPANIONS: dict[str, int] = {'FIELDEND': 1}

# Inline controls counted before/after. Anything not listed is not a control
# for this check (its text still takes part in the body-text comparison).
CONTROL_TAGS = frozenset({
    'TABLE', 'PICTURE', 'LINE', 'RECTANGLE', 'ELLIPSE', 'ARC', 'POLYGON', 'CURVE', 'CONNECTLINE',
    'UNKNOWNOBJECT', 'CONTAINER', 'OLE', 'EQUATION', 'TEXTART', 'BUTTON', 'RADIOBUTTON', 'CHECKBUTTON',
    'COMBOBOX', 'EDIT', 'LISTBOX', 'SCROLLBAR', 'FIELDBEGIN', 'FIELDEND', 'BOOKMARK', 'HEADER', 'FOOTER',
    'FOOTNOTE', 'ENDNOTE', 'AUTONUM', 'NEWNUM', 'PAGENUMCTRL', 'PAGEHIDING', 'PAGENUM', 'INDEXMARK',
    'COMPOSE', 'DUTMAL', 'HIDDENCOMMENT', 'SECDEF', 'COLDEF',
})

# Elements whose subtree holds the visible run text of a paragraph.
TEXT_TAGS = frozenset({'CHAR'})
PARAGRAPH_TAG = 'P'
BODY_TAG = 'BODY'

# Attribute / child names used by per-kind checks.
ATTRS: dict[str, str] = {
    'field_type': 'Type',
    'field_command': 'Command',
    'bookmark_name': 'Name',
    'equation_script_tags': 'SCRIPT,script',
    'shape_size_tag': 'SIZE',
    'shape_width': 'Width',
    'shape_height': 'Height',
    'shape_position_tag': 'POSITION',
    'shape_treat_as_char': 'TreatAsChar',
    'header_apply': 'ApplyPageType',
}
APPLY_PAGE_TYPES: dict[str, str] = {'both': 'Both', 'even': 'Even', 'odd': 'Odd'}

# Native entry points per kind. 'run' kinds use HAction.Run; 'execute' kinds use
# HAction.GetDefault + HSet.SetItem + HAction.Execute with the named parameter set.
NATIVE: dict[str, dict[str, Any]] = {
    'footnote': {'mode': 'run', 'action': 'InsertFootnote', 'types_text': True, 'closes': True},
    'endnote': {'mode': 'run', 'action': 'InsertEndnote', 'types_text': True, 'closes': True},
    'memo': {'mode': 'run', 'action': 'InsertFieldMemo', 'types_text': True, 'closes': True},
    'hyperlink': {'mode': 'execute', 'action': 'InsertHyperlink', 'pset': 'HHyperLink'},
    'bookmark': {'mode': 'execute', 'action': 'Bookmark', 'pset': 'HBookMark'},
    'equation': {'mode': 'execute', 'action': 'EquationCreate', 'pset': 'HEqEdit'},
    'line': {'mode': 'execute', 'action': 'DrawObjCreatorLine', 'pset': 'HShapeObject'},
    'rectangle': {'mode': 'execute', 'action': 'DrawObjCreatorRectangle', 'pset': 'HShapeObject'},
    'ellipse': {'mode': 'execute', 'action': 'DrawObjCreatorEllipse', 'pset': 'HShapeObject'},
    'header': {'mode': 'execute', 'action': 'HeaderFooter', 'pset': 'HHeaderFooter', 'types_text': True, 'closes': True},
    'footer': {'mode': 'execute', 'action': 'HeaderFooter', 'pset': 'HHeaderFooter', 'types_text': True, 'closes': True},
}
CLOSE_ACTION = 'CloseEx'
EQUATION_VERSION = 'Equation Version 60'
# HeaderFooter parameter items (unverified; all "which one" aliases get the same value).
HEADER_FOOTER_TYPE_ITEMS = ('Type', 'HeaderFooterCtrlType', 'CtrlType')
HEADER_FOOTER_APPLY_ITEM = 'ApplyTo'
HEADER_FOOTER_APPLY_VALUES: dict[str, int] = {'both': 0, 'even': 1, 'odd': 2}

# Per-kind parameters. None = required; anything else is the default.
KIND_PARAMS: dict[str, dict[str, Any]] = {
    'footnote': {'text': None},
    'endnote': {'text': None},
    'memo': {'text': None},
    'hyperlink': {'url': None, 'display_text': None},
    'bookmark': {'name': None},
    'equation': {'script': None},
    'line': {'width_mm': None, 'height_mm': None, 'treat_as_char': True},
    'rectangle': {'width_mm': None, 'height_mm': None, 'treat_as_char': True},
    'ellipse': {'width_mm': None, 'height_mm': None, 'treat_as_char': True},
    'header': {'text': None, 'apply_to': 'both'},
    'footer': {'text': None, 'apply_to': 'both'},
}
KINDS = frozenset(KIND_PARAMS)
PARAM_KEYS = tuple(sorted({key for params in KIND_PARAMS.values() for key in params}))
SHAPE_KINDS = frozenset({'line', 'rectangle', 'ellipse'})
# 'none': strict normal edit state; 'required': a live selection; 'optional': either.
SELECTION_POLICY: dict[str, str] = {kind: 'none' for kind in KINDS} | {'hyperlink': 'required', 'memo': 'optional'}

MAX_TEXT = 2000
MAX_URL = 2000
MAX_BOOKMARK = 40
MAX_SCRIPT = 4000
MAX_MM = 1000.0
MAX_CANDIDATES = 500
URL_SCHEMES = frozenset({'http', 'https', 'mailto'})
HWPUNIT_PER_MM = 7200 / 25.4


class ObjectInsertError(ValueError):
    """A step, plan or readback cannot be accepted."""


def mm_to_hwpunit(value: float) -> int:
    return int(round(float(value) * HWPUNIT_PER_MM))


def _has_control_chars(value: str, *, allow: str = '') -> bool:
    return any(unicodedata.category(char) == 'Cc' and char not in allow for char in value)


def _text(step: Mapping[str, Any], key: str, *, max_chars: int, allow_controls: str = '') -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ObjectInsertError(f'{key} must be a non-empty string')
    if len(value) > max_chars:
        raise ObjectInsertError(f'{key} must be at most {max_chars} characters')
    if _has_control_chars(value, allow=allow_controls):
        raise ObjectInsertError(f'{key} must not contain control characters')
    return value


def _url(value: str) -> str:
    if any(char.isspace() for char in value) or ';' in value or '|' in value:
        raise ObjectInsertError('url must not contain whitespace, ";" or "|" (they break the hyperlink Command format)')
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in URL_SCHEMES:
        raise ObjectInsertError('url scheme must be http, https or mailto')
    if scheme in ('http', 'https') and not parts.netloc:
        raise ObjectInsertError('http(s) url needs a host')
    if scheme == 'mailto' and not parts.path:
        raise ObjectInsertError('mailto url needs an address')
    return value


def _mm(step: Mapping[str, Any], key: str) -> float:
    value = step.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ObjectInsertError(f'{key} must be a number of millimetres')
    if not (0 <= value <= MAX_MM):
        raise ObjectInsertError(f'{key} must be 0..{MAX_MM:g} mm')
    return float(value)


def normalize_expected_pos(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ObjectInsertError('expected_pos must be [list, para, pos] from where')
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value):
        raise ObjectInsertError('expected_pos entries must be non-negative integers')
    return [int(item) for item in value]


def normalize_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate the kind-specific part of a step; return the plan.

    Raises ObjectInsertError with a caller-facing reason. Fields that belong to
    another kind are rejected, not ignored.
    """
    kind = step.get('kind')
    if not isinstance(kind, str) or kind not in KINDS:
        raise ObjectInsertError(f'kind must be one of: {", ".join(sorted(KINDS))}')
    if step.get('confirm_mutation') is not True:
        raise ObjectInsertError('requires confirm_mutation=true')
    params = KIND_PARAMS[kind]
    stray = sorted(key for key in PARAM_KEYS if key not in params and step.get(key) is not None)
    if stray:
        raise ObjectInsertError(f'kind {kind} does not take: {", ".join(stray)}')
    plan: dict[str, Any] = {'kind': kind, 'expected_pos': normalize_expected_pos(step.get('expected_pos'))}
    if 'text' in params:
        text = _text(step, 'text', max_chars=MAX_TEXT)
        plan['text'] = text
    if kind == 'hyperlink':
        plan['url'] = _url(_text(step, 'url', max_chars=MAX_URL))
        plan['display_text'] = _text(step, 'display_text', max_chars=MAX_TEXT)
    elif kind == 'bookmark':
        plan['name'] = _text(step, 'name', max_chars=MAX_BOOKMARK)
    elif kind == 'equation':
        plan['script'] = _text(step, 'script', max_chars=MAX_SCRIPT, allow_controls='\n\t')
    elif kind in SHAPE_KINDS:
        width, height = _mm(step, 'width_mm'), _mm(step, 'height_mm')
        if kind == 'line' and width == 0 and height == 0:
            raise ObjectInsertError('a line needs a non-zero width_mm or height_mm')
        if kind != 'line' and (width == 0 or height == 0):
            raise ObjectInsertError(f'{kind} needs positive width_mm and height_mm')
        treat = step.get('treat_as_char', True)
        if treat is None:
            treat = True
        if not isinstance(treat, bool):
            raise ObjectInsertError('treat_as_char must be a boolean')
        plan.update({'width_mm': width, 'height_mm': height, 'treat_as_char': treat,
                     'width_hu': mm_to_hwpunit(width), 'height_hu': mm_to_hwpunit(height)})
    elif kind in ('header', 'footer'):
        apply_to = step.get('apply_to') or 'both'
        if apply_to not in APPLY_PAGE_TYPES:
            raise ObjectInsertError('apply_to must be one of: both, even, odd')
        plan['apply_to'] = apply_to
    tag, field_type = KIND_TAGS[kind]
    plan['tag_key'] = control_key_for(tag, field_type)
    plan['companions'] = dict(FIELD_COMPANIONS) if tag == 'FIELDBEGIN' else {}
    plan['selection_policy'] = SELECTION_POLICY[kind]
    plan['native'] = dict(NATIVE[kind])
    return plan


def native_items(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Parameter-set items for 'execute' kinds, in the order they are set."""
    kind = plan['kind']
    if kind == 'hyperlink':
        # External link format, as recorded by Hancom's macro recorder:
        # "<url>;1;0;0;" (type 1 = web address). pyhwpx insert_hyperlink only
        # covers bookmark targets ("?<name>|<desc>;0;0;0;").
        return {'Command': f'{plan["url"]};1;0;0;'}
    if kind == 'bookmark':
        return {'Name': plan['name']}
    if kind == 'equation':
        return {'string': plan['script'], 'Version': EQUATION_VERSION}
    if kind in SHAPE_KINDS:
        return {'Width': plan['width_hu'], 'Height': plan['height_hu'], 'TreatAsChar': 1 if plan['treat_as_char'] else 0}
    if kind in ('header', 'footer'):
        value = 0 if kind == 'header' else 1
        items: dict[str, Any] = {name: value for name in HEADER_FOOTER_TYPE_ITEMS}
        items[HEADER_FOOTER_APPLY_ITEM] = HEADER_FOOTER_APPLY_VALUES[plan['apply_to']]
        return items
    return {}


def public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Plan for responses: text-like values are reported by length only."""
    hidden = {'text', 'display_text', 'script'}
    out = {key: value for key, value in plan.items() if key not in hidden}
    for key in hidden & set(plan):
        out[f'{key}_chars'] = len(plan[key])
    return out


# ---------------------------------------------------------------- HWPML readback


def control_key_for(tag: str, field_type: str | None) -> str:
    return f'{tag}:{field_type or ""}' if tag == 'FIELDBEGIN' else tag


def control_key(element: ET.Element) -> str | None:
    if element.tag not in CONTROL_TAGS:
        return None
    field_type = element.get(ATTRS['field_type']) if element.tag == 'FIELDBEGIN' else None
    return control_key_for(element.tag, field_type)


def parse_hwpml(xml_text: Any) -> ET.Element:
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise ObjectInsertError('document readback returned no HWPML text')
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', xml_text.lstrip('﻿'), count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ObjectInsertError(f'document readback is not well-formed HWPML: {exc}') from exc
    return root


def _body(root: ET.Element) -> ET.Element:
    if root.tag == BODY_TAG:
        return root
    body = root.find(f'.//{BODY_TAG}')
    return body if body is not None else root


def summarize(root: ET.Element, skip: ET.Element | None = None) -> dict[str, Any]:
    """Control counts and whitespace-normalized run text of the body, without ``skip``'s subtree."""
    counts: Counter[str] = Counter()
    paragraphs: list[str] = []
    char_elements = 0

    def walk(element: ET.Element, sink: list[str] | None) -> None:
        nonlocal char_elements
        if element is skip:
            return
        key = control_key(element)
        if key is not None:
            counts[key] += 1
        if element.tag == PARAGRAPH_TAG:
            own: list[str] = []
            for child in element:
                walk(child, own)
            paragraphs.append(''.join(own))
            return
        if element.tag in TEXT_TAGS and sink is not None:
            char_elements += 1
            _collect_run_text(element, sink, skip)
            for child in element:
                walk_controls_only(child)
            return
        for child in element:
            walk(child, sink)

    def walk_controls_only(element: ET.Element) -> None:
        if element is skip:
            return
        key = control_key(element)
        if key is not None:
            counts[key] += 1
        for child in element:
            walk_controls_only(child)

    walk(_body(root), None)
    text = ' '.join(' '.join(paragraphs).split())
    return {'counts': counts, 'text': text, 'char_elements': char_elements}


def _collect_run_text(element: ET.Element, sink: list[str], skip: ET.Element | None) -> None:
    if element.text:
        sink.append(element.text)
    for child in element:
        if child is not skip:
            _collect_run_text(child, sink, skip)
        if child.tail:
            sink.append(child.tail)


def _norm(value: str) -> str:
    return ' '.join(value.split())


def element_text(element: ET.Element) -> str:
    return _norm(''.join(element.itertext()))


TEXT_RUN_TAG = 'TEXT'
CHAR_SHAPE_ATTR = 'CharShape'
MAX_DESCENT = 16
_attrs = attrs


def linearize(container: ET.Element) -> list[tuple[tuple[Any, ...], ET.Element | None]]:
    """Document-order tokens under ``container`` (not including it).

    ``('E', tag, attrs)`` / ``('/E', tag)`` open and close every element other
    than the run wrappers TEXT/CHAR (paragraphs, table rows, cells, sizes),
    ``('T', char, char_shape)`` is one run character, and ``('C', key,
    signature)`` is a whole control, which is atomic here (see
    ``evaluate_insert`` for descending into one). Each token
    is paired with its element (controls) or None.
    """
    tokens: list[tuple[tuple[Any, ...], ET.Element | None]] = []

    def chars(value: str | None, shape: str | None) -> None:
        for char in value or '':
            tokens.append((('T', char, shape), None))

    def walk(element: ET.Element, shape: str | None, in_run: bool) -> None:
        key = control_key(element)
        if key is not None:
            tokens.append((('C', key, signature(element)), element))
            return
        # Run wrappers (TEXT, CHAR) may split or merge around a new control, so
        # they carry no token of their own; every other element opens and
        # closes with a token carrying its attributes (table/cell size, spans,
        # border fills, paragraph shape).
        structural = element.tag not in TEXT_TAGS and element.tag != TEXT_RUN_TAG
        if structural:
            tokens.append((('E', element.tag, _attrs(element)), None))
        if element.tag == TEXT_RUN_TAG:
            shape = element.get(CHAR_SHAPE_ATTR, shape)
        run = in_run or element.tag in TEXT_TAGS
        if element.tag in TEXT_TAGS:
            chars(element.text, shape)
        for child in element:
            walk(child, shape, run)
            if run:
                chars(child.tail, shape)
        if structural:
            tokens.append((('/E', element.tag), None))

    for child in container:
        walk(child, None, False)
    return tokens


def _middle(before: list[Any], after: list[Any]) -> tuple[int, list[Any], list[Any]]:
    """Strip the longest common prefix and suffix (by token); return (prefix length, before middle, after middle)."""
    limit = min(len(before), len(after))
    prefix = 0
    while prefix < limit and before[prefix][0] == after[prefix][0]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and before[len(before) - 1 - suffix][0] == after[len(after) - 1 - suffix][0]:
        suffix += 1
    return prefix, before[prefix:len(before) - suffix], after[prefix:len(after) - suffix]


def _field_command_target(command: str) -> str:
    """First ';' segment of a field Command with backslash escapes (e.g. ``\\:``) removed."""
    first = command.split(';', 1)[0]
    return re.sub(r'\\(.)', r'\1', first)


def _element_reasons(plan: Mapping[str, Any], element: ET.Element) -> list[str]:
    kind = plan['kind']
    reasons: list[str] = []
    if 'text' in plan and element_text(element) != _norm(plan['text']):
        reasons.append(f'new {element.tag} text is not exactly the requested text')
    if kind == 'hyperlink' and _field_command_target(element.get(ATTRS['field_command']) or '') != plan['url']:
        reasons.append('new hyperlink Command does not name exactly the url')
    if kind == 'bookmark' and element.get(ATTRS['bookmark_name']) != plan['name']:
        reasons.append('new BOOKMARK Name differs from the requested name')
    if kind == 'equation':
        scripts = [child for tag in ATTRS['equation_script_tags'].split(',') for child in element.iter(tag)]
        if len(scripts) != 1 or (scripts[0].text or '').strip() != plan['script'].strip():
            reasons.append('new EQUATION script differs from the requested script')
    if kind in SHAPE_KINDS:
        size = element.find(f'.//{ATTRS["shape_size_tag"]}')
        for attr, want in ((ATTRS['shape_width'], plan['width_hu']), (ATTRS['shape_height'], plan['height_hu'])):
            raw = size.get(attr) if size is not None else None
            try:
                got = int(raw) if raw is not None else None
            except ValueError:
                got = None
            if got is None or abs(got - want) > max(2, want * 0.01):
                reasons.append(f'new {element.tag} {attr} is {raw!r}, expected about {want} HWPUNIT')
        position = element.find(f'.//{ATTRS["shape_position_tag"]}')
        treat = (position.get(ATTRS['shape_treat_as_char']) if position is not None else None) or ''
        if treat.lower() != ('true' if plan['treat_as_char'] else 'false'):
            reasons.append(f'new {element.tag} TreatAsChar is {treat!r}, expected {plan["treat_as_char"]}')
    if kind in ('header', 'footer') and element.get(ATTRS['header_apply']) != APPLY_PAGE_TYPES[plan['apply_to']]:
        reasons.append(f'new {element.tag} {ATTRS["header_apply"]} is {element.get(ATTRS["header_apply"])!r}, expected {APPLY_PAGE_TYPES[plan["apply_to"]]!r}')
    return reasons


def check_before(plan: Mapping[str, Any], before: ET.Element) -> dict[str, Any]:
    """Refuse plans the readback cannot verify. Returns the before summary."""
    summary = summarize(before)
    if summary['char_elements'] == 0 and _norm(''.join(_body(before).itertext())):
        raise ObjectInsertError('document readback holds text outside CHAR runs; this HWPML layout cannot be verified')
    if plan['kind'] in ('header', 'footer'):
        tag = KIND_TAGS[plan['kind']][0]
        want = APPLY_PAGE_TYPES[plan['apply_to']]
        for element in _body(before).iter(tag):
            apply = element.get(ATTRS['header_apply'])
            if apply in (None, want, APPLY_PAGE_TYPES['both']) or want == APPLY_PAGE_TYPES['both']:
                raise ObjectInsertError(
                    f'document already has a {tag} that may cover {plan["apply_to"]} pages ({apply!r}); '
                    'replacing an existing header/footer is not supported, delete it explicitly first'
                )
    if plan['kind'] == 'bookmark':
        for element in _body(before).iter(KIND_TAGS['bookmark'][0]):
            if element.get(ATTRS['bookmark_name']) == plan['name']:
                raise ObjectInsertError('a bookmark with this name already exists')
    return summary


def _control_tally(tokens: list[Any]) -> Counter[str]:
    return Counter(token[1] for token, _element in tokens if token[0] == 'C')


def _locate_insert(plan: Mapping[str, Any], before_tokens: list[Any], after_tokens: list[Any], path: list[str]) -> tuple[ET.Element | None, list[str]]:
    """Find the one inserted control; returns (element, reasons). Descends into one changed container."""
    key = plan['tag_key']
    field = bool(plan.get('companions'))
    prefix, old, new = _middle(before_tokens, after_tokens)
    if not old and not new:
        return None, ['the document is unchanged']
    if (
        len(old) == 1 and len(new) == 1 and old[0][0][0] == 'C' and new[0][0][0] == 'C'
        and old[0][1].tag == new[0][1].tag and _attrs(old[0][1]) == _attrs(new[0][1])
    ):
        if len(path) >= MAX_DESCENT:
            return None, [f'the change is nested deeper than {MAX_DESCENT} controls']
        path.append(old[0][1].tag)
        return _locate_insert(plan, linearize(old[0][1]), linearize(new[0][1]), path)
    reasons: list[str] = []
    head = new[0] if new else None
    starts_ok = head is not None and head[0][0] == 'C' and head[0][1] == key
    if field:
        tail = new[-1] if len(new) >= 2 else None
        inner, wrapped = new[1:-1], old
        if not starts_ok:
            reasons.append(f'the change does not start with the new {key}')
        if tail is None or tail[0][0] != 'C' or tail[1].tag != 'FIELDEND':
            reasons.append('the new field is not closed by its own FIELDEND right after the wrapped text')
        elif tail[1].get(ATTRS['field_type']) not in (None, KIND_TAGS[plan['kind']][1]):
            reasons.append(f'the new FIELDEND Type is {tail[1].get(ATTRS["field_type"])!r}')
        elif any(token[0] != 'T' for token, _element in inner + wrapped):
            reasons.append('the new field wraps something other than plain text, or other content changed')
        elif [token[1] for token, _element in inner] != [token[1] for token, _element in wrapped]:
            reasons.append('document text outside the new object changed')
        elif plan['kind'] == 'hyperlink' and ''.join(token[1] for token, _element in inner) != plan['display_text']:
            reasons.append('the new hyperlink does not wrap exactly display_text')
    elif not (starts_ok and len(new) == 1 and not old):
        added = _control_tally(new)
        if added[key] > 0:
            added[key] -= 1
        changed = {name: added[name] - _control_tally(old)[name] for name in set(added) | set(_control_tally(old))}
        changed = {name: value for name, value in sorted(changed.items()) if value}
        if changed:
            reasons.append(f'other controls changed: {changed}')
        changed_elements = sorted({token[1] for token, _element in new + old if token[0] in ('E', '/E')})
        if PARAGRAPH_TAG in changed_elements:
            reasons.append('paragraph structure changed')
        if [tag for tag in changed_elements if tag != PARAGRAPH_TAG]:
            reasons.append(f'container structure or attributes changed: {[tag for tag in changed_elements if tag != PARAGRAPH_TAG]}')
        if any(token[0] == 'T' for token, _element in new + old):
            reasons.append('document text outside the new object changed')
        if not reasons:
            reasons.append(f'the change is not exactly one new {key}: content next to it changed')
    if reasons:
        return None, reasons
    assert head is not None
    own = _element_reasons(plan, head[1])
    if own:
        return None, own
    path.append(f'token {prefix}')
    return head[1], []


def evaluate_insert(plan: Mapping[str, Any], before: ET.Element, after: ET.Element) -> dict[str, Any]:
    """Prove the after-document is the before-document plus exactly one new object.

    Both readbacks are linearized into document-order tokens (paragraph
    starts with their attributes, run characters with their CharShape,
    controls as whole-subtree signatures). The after tokens must equal the
    before tokens with a single contiguous insertion: the new control (for a
    field, FIELDBEGIN ... FIELDEND around the unchanged wrapped text). When
    the caret sat inside a control (a table cell, a note), that one control
    is descended into and the same rule applies there. The requested
    text/url/name/script/size is checked on the control found at that
    position, so a pre-existing twin can never stand in for it. Outside
    BODY, HEAD may only gain appended list entries and everything else
    (TAIL with its BinData) must be unchanged. ``reasons`` lists up to ten
    problems.
    """
    key = plan['tag_key']
    companions: Mapping[str, int] = plan.get('companions') or {}
    base = summarize(before)
    full = summarize(after)
    delta = {name: full['counts'][name] - base['counts'][name] for name in set(full['counts']) | set(base['counts'])}
    delta = {name: value for name, value in sorted(delta.items()) if value}
    reasons: list[str] = []
    matched: ET.Element | None = None
    path: list[str] = []
    if delta.get(key, 0) != 1:
        reasons.append(f'{key} count changed by {delta.get(key, 0)}, expected exactly +1')
    for name, want in companions.items():
        if delta.get(name, 0) != want:
            reasons.append(f'{name} count changed by {delta.get(name, 0)}, expected exactly +{want}')
    if not reasons:
        matched, found = _locate_insert(plan, linearize(_body(before)), linearize(_body(after)), path)
        reasons.extend(found)
    reasons.extend(head_tail_reasons(before, after))
    return {
        'ok': not reasons,
        'target': key,
        'expected_delta': {key: 1, **companions},
        'observed_delta': delta,
        'inserted_at': path if matched is not None else None,
        'matched_text_chars': len(element_text(matched)) if matched is not None else None,
        'reasons': reasons[:10],
    }


def public_counts(summary: Mapping[str, Any]) -> dict[str, int]:
    return dict(sorted(summary['counts'].items()))
