"""Whole-document HWPML invariants shared by the exact object and layout verifiers.

Pure functions over two parsed ``GetTextFile('HWPML2X', '')`` readbacks. They
fail closed: anything they cannot explain is returned as a reason.

* ``signature``: canonical subtree form, ignoring only ``VOLATILE_ATTRS``.
* ``head_tail_reasons``: HEAD may only *grow* (new entries appended at the end
  of a homogeneous list such as a char/para shape list, with that list's
  ``Count`` attribute following); no existing HEAD entry may change. Every
  other top-level element except BODY (TAIL with BINDATASTORAGE included)
  must be unchanged.

The attribute names here are HWPML 2.x names and are UNVERIFIED natively.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping

# Attributes that may legitimately differ between two readbacks of an unchanged
# element (per tag; '*' applies to every tag). UNVERIFIED natively: InstId is
# Hancom's per-instance id and AUTONUM Number renumbers when a note is added
# before an existing one.
VOLATILE_ATTRS: dict[str, frozenset[str]] = {'*': frozenset({'InstId'}), 'AUTONUM': frozenset({'Number'})}
# Attribute that counts a HEAD list's entries; it may follow appended entries.
LIST_COUNT_ATTR = 'Count'
BODY_TAG = 'BODY'
HEAD_TAG = 'HEAD'
MAX_REASONS = 5


def attrs(element: ET.Element, ignore: Mapping[str, frozenset[str]] | None = None) -> tuple[tuple[str, str], ...]:
    skip = VOLATILE_ATTRS['*'] | VOLATILE_ATTRS.get(element.tag, frozenset())
    if ignore:
        skip = skip | ignore.get(element.tag, frozenset())
    return tuple(sorted((key, value) for key, value in element.attrib.items() if key not in skip))


def signature(element: ET.Element, ignore: Mapping[str, frozenset[str]] | None = None) -> str:
    """Canonical form of an element's whole subtree, text and tails included.

    ``ignore`` names further per-tag attributes a caller checks elsewhere.
    """
    own = ''.join(f' {key}={value!r}' for key, value in attrs(element, ignore))
    children = ''.join(signature(child, ignore) + f'~{child.tail or ""!r}' for child in element)
    return f'<{element.tag}{own}>{element.text or ""!r}{children}</{element.tag}>'


def _head_growth_reasons(before: ET.Element, after: ET.Element, path: str, allow: Mapping[str, Iterable[str]], out: list[str]) -> None:
    if len(out) >= MAX_REASONS:
        return
    where = f'{path}/{before.tag}'
    if after.tag != before.tag:
        out.append(f'{where} became {after.tag}')
        return
    old_children, new_children = list(before), list(after)
    appended = new_children[len(old_children):]
    free = set(allow.get(before.tag, ())) | ({LIST_COUNT_ATTR} if appended else set())
    old_attrs = {key: value for key, value in attrs(before) if key not in free}
    new_attrs = {key: value for key, value in attrs(after) if key not in free}
    if old_attrs != new_attrs:
        changed = sorted(key for key in set(old_attrs) | set(new_attrs) if old_attrs.get(key) != new_attrs.get(key))
        out.append(f'{where} attributes changed: {changed}')
    if (before.text or '').strip() != (after.text or '').strip():
        out.append(f'{where} text changed')
    if len(new_children) < len(old_children):
        out.append(f'{where} lost {len(old_children) - len(new_children)} entries')
    if appended:
        tags = {child.tag for child in old_children + appended}
        if len(tags) != 1:
            out.append(f'{where} gained entries outside a homogeneous list: {sorted({child.tag for child in appended})}')
    for index, (old, new) in enumerate(zip(old_children, new_children)):
        _head_growth_reasons(old, new, f'{where}[{index}]', allow, out)


def head_tail_reasons(before_root: ET.Element, after_root: ET.Element, *, allow_head_attrs: Mapping[str, Iterable[str]] | None = None) -> list[str]:
    """Reasons HEAD did not only grow, or anything else outside BODY changed.

    ``allow_head_attrs`` names HEAD attributes an edit may legitimately change
    (tag -> attribute names), e.g. a section count after adding a section.
    """
    reasons: list[str] = []
    before_parts = [child for child in before_root if child.tag != BODY_TAG]
    after_parts = [child for child in after_root if child.tag != BODY_TAG]
    if [child.tag for child in before_parts] != [child.tag for child in after_parts]:
        return [f'top-level HWPML parts changed: {[c.tag for c in before_parts]} -> {[c.tag for c in after_parts]}']
    for old, new in zip(before_parts, after_parts):
        if old.tag == HEAD_TAG:
            found: list[str] = []
            _head_growth_reasons(old, new, '', allow_head_attrs or {}, found)
            reasons.extend(f'existing HEAD entry changed: {item}' for item in found)
        elif signature(old) != signature(new):
            reasons.append(f'{old.tag} changed (it holds embedded binary data and document-level parts that must not change)')
    return reasons[:MAX_REASONS]
