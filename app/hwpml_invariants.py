"""Whole-document HWPML invariants shared by the exact object and layout verifiers.

Pure functions over two parsed ``GetTextFile('HWPML2X', '')`` readbacks. They
fail closed: anything they cannot explain is returned as a reason.

* ``signature``: canonical subtree form, ignoring only ``VOLATILE_ATTRS``.
* ``head_tail_reasons``: HEAD may only *grow* (new entries appended at the end
  of a ``GROWABLE_HEAD_LISTS`` list, with that list's ``Count`` attribute
  following); no existing HEAD element or entry may change at all. The
  ``REFERENCE_BOUND_LISTS`` a section owns change only by the entries a
  ``DefinitionDelta`` names (``section_insert_definitions`` and
  ``section_delete_definitions`` bind them to the edited section's own SECDEF).
  Every other top-level element except BODY (TAIL with BINDATASTORAGE
  included) must be unchanged.

Attribute and element names are HWPML 2.x names; where one was read back
natively the comment next to it says so, the others are UNVERIFIED natively.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from typing import Any

# Attributes that may legitimately differ between two readbacks of an unchanged
# element (per tag; '*' applies to every tag). UNVERIFIED natively: InstId is
# Hancom's per-instance id and AUTONUM Number renumbers when a note is added
# before an existing one.
VOLATILE_ATTRS: dict[str, frozenset[str]] = {'*': frozenset({'InstId'}), 'AUTONUM': frozenset({'Number'})}
# Attribute that counts a HEAD list's entries; it may follow appended entries.
LIST_COUNT_ATTR = 'Count'
# Attribute that identifies a HEAD list entry (referenced as CharShape="n" etc.).
ENTRY_ID_ATTR = 'Id'
# HEAD lists any exact edit may append entries to (list tag -> entry tag): a new char shape for a link, a paragraph
# shape or tab definition for an indent, a border fill for a drawing object. UNVERIFIED natively; nothing else in HEAD
# may change, except the REFERENCE_BOUND_LISTS below by a section edit.
GROWABLE_HEAD_LISTS: dict[str, str] = {
    'CHARSHAPELIST': 'CHARSHAPE',
    'PARASHAPELIST': 'PARASHAPE',
    'TABDEFLIST': 'TABDEF',
    'BORDERFILLLIST': 'BORDERFILL',
}
# HEAD definition lists a section owns through its SECDEF (list tag -> entry tag). As read back on Hancom 2024: a
# section break appends a NUMBERING and adds or appends a memo shape (element MEMO) for the new section, a section
# delete drops the ones only the removed section used, and inserting a memo adds none. They change only by the entry
# Ids a DefinitionDelta names.
REFERENCE_BOUND_LISTS: dict[str, str] = {'NUMBERINGLIST': 'NUMBERING', 'MEMOSHAPELIST': 'MEMO'}
# MAPPINGTABLE child order as read back on Hancom 2024 (tests/fixtures/native_ordinary/listkinds.hwpml2x, and a section
# break that added MEMOSHAPELIST after STYLELIST). A reference-bound list that appears must take its place in this order
# among the existing children; next to a child not listed here its place is unknown and it is refused.
MAPPINGTABLE_ORDER = ('BINDATALIST', 'FACENAMELIST', 'BORDERFILLLIST', 'CHARSHAPELIST', 'TABDEFLIST', 'NUMBERINGLIST', 'BULLETLIST',
                      'PARASHAPELIST', 'STYLELIST', 'MEMOSHAPELIST')
MAPPINGTABLE_TAG = 'MAPPINGTABLE'


@dataclass(frozen=True)
class DefinitionDelta:
    """What one section edit may change in the REFERENCE_BOUND_LISTS, per list tag: entry Ids it creates (appended at
    the list's end, or as a new MAPPINGTABLE list) and entry Ids it may remove. The default allows no change."""

    created: dict[str, frozenset[str]] = field(default_factory=dict)
    removable: dict[str, frozenset[str]] = field(default_factory=dict)


BODY_TAG = 'BODY'
HEAD_TAG = 'HEAD'
MAX_REASONS = 5
# Whole-document readback limits. A bundle has 120 s in total and the proof
# needs one readback + verification before the edit and one after. Before
# mutating, an edit reads the document and dry-runs the full verification on
# it; if that exceeds PROOF_BUDGET_SECONDS the after-edit proof may not finish
# in time, so the edit is refused while nothing has changed yet. Portable
# measurement: a text-dense 4M-character readback verifies in about 6 s and
# 0.6 GB peak memory; slower machines are caught by the timed dry run.
MAX_READBACK_CHARS = 4 * 1024 * 1024
PROOF_BUDGET_SECONDS = 30.0


def readback_size_reason(xml_text: object) -> str | None:
    if isinstance(xml_text, str) and len(xml_text) > MAX_READBACK_CHARS:
        return f'HWPML readback is {len(xml_text)} characters, above the {MAX_READBACK_CHARS} limit for exact verification'
    return None


def before_budget_reason(seconds: float) -> str | None:
    if seconds > PROOF_BUDGET_SECONDS:
        return (f'the before-edit readback plus a dry run of the proof took {seconds:.1f}s (budget {PROOF_BUDGET_SECONDS:.0f}s); '
                'the after-edit proof might not finish within the bundle time limit')
    return None


def attrs(element: ET.Element, ignore: Mapping[str, frozenset[str]] | None = None) -> tuple[tuple[str, str], ...]:
    skip = VOLATILE_ATTRS['*'] | VOLATILE_ATTRS.get(element.tag, frozenset())
    if ignore:
        skip = skip | ignore.get(element.tag, frozenset())
    return tuple(sorted((key, value) for key, value in element.attrib.items() if key not in skip))


def signature(
    element: ET.Element,
    ignore: Mapping[str, frozenset[str]] | None = None,
    omit: frozenset[str] = frozenset(),
    cache: dict[int, str] | None = None,
    cache_tags: frozenset[str] = frozenset(),
) -> str:
    """Canonical form of an element's whole subtree, text and tails included.

    ``ignore`` names further per-tag attributes, and ``omit`` whole child
    subtrees (by tag), that a caller checks elsewhere. With ``cache``, the
    signature of every element whose tag is in ``cache_tags`` is kept by
    ``id`` so a nested control is serialized once per tree, not once per
    level (the cache must only be used with one tree and one ignore/omit).
    """
    if cache is not None and element.tag in cache_tags:
        known = cache.get(id(element))
        if known is not None:
            return known
    own = ''.join(f' {key}={value!r}' for key, value in attrs(element, ignore))
    children = ''.join(
        signature(child, ignore, omit, cache, cache_tags) + f'~{child.tail or ""!r}' for child in element if child.tag not in omit
    )
    result = f'<{element.tag}{own}>{element.text or ""!r}{children}</{element.tag}>'
    if cache is not None and element.tag in cache_tags:
        cache[id(element)] = result
    return result


def _list_growth_reasons(where: str, before: ET.Element, after: ET.Element, old_len: int, appended: list[ET.Element]) -> list[str]:
    """A grown list must stay self-consistent: its Count follows exactly and new Ids are new and sequential."""
    reasons: list[str] = []
    old_count, new_count = before.get(LIST_COUNT_ATTR), after.get(LIST_COUNT_ATTR)
    if old_count is not None or new_count is not None:
        try:
            expected = int(old_count) + len(appended) if old_count is not None else None
            got = int(new_count) if new_count is not None else None
        except ValueError:
            expected, got = None, -1
        if got != expected:
            reasons.append(f'{where} {LIST_COUNT_ATTR} is {new_count!r} after appending {len(appended)} entries to {old_count!r}')
    old_ids = [child.get(ENTRY_ID_ATTR) for child in list(after)[:old_len]]
    new_ids = [child.get(ENTRY_ID_ATTR) for child in appended]
    if any(value is not None for value in old_ids + new_ids):
        if None in new_ids or len(set(new_ids)) != len(new_ids) or set(new_ids) & set(old_ids):
            reasons.append(f'{where} appended entries whose {ENTRY_ID_ATTR}s are missing, repeated or already used: {new_ids}')
        elif old_ids == [str(index) for index in range(old_len)] and new_ids != [str(index) for index in range(old_len, old_len + len(appended))]:
            reasons.append(f'{where} appended {ENTRY_ID_ATTR}s {new_ids} do not continue 0..{old_len - 1}')
    return reasons


# HEAD elements that record editor view state, not document content: DOCSETTING/CARETPOS holds the caret's list,
# paragraph and position, so it follows every caret move (on Hancom 2024 a footnote insert and a section break both
# changed only CARETPOS in HEAD). Their attributes are not compared; their structure still is.
VIEW_STATE_HEAD_TAGS = frozenset({'CARETPOS'})


def _bound_entries_problem(element: ET.Element, entry_tag: str) -> str | None:
    """Why a reference-bound list cannot be read exactly: foreign entries, a missing, non-numeric or repeated Id, or a
    Count that is not its number of entries. None when it is well-formed."""
    entries = list(element)
    if any(entry.tag != entry_tag for entry in entries):
        return f'it holds entries other than {entry_tag}'
    ids = [(entry.get(ENTRY_ID_ATTR) or '').strip() for entry in entries]
    if any(not value.isdigit() for value in ids):
        return f'an entry has no numeric {ENTRY_ID_ATTR}'
    if len(set(ids)) != len(ids):
        return f'an entry {ENTRY_ID_ATTR} is repeated'
    count = element.get(LIST_COUNT_ATTR)
    if (count or '').strip() != str(len(entries)):
        return f'{LIST_COUNT_ATTR} {count!r} is not its {len(entries)} entries'
    return None


def _entry_ids(element: ET.Element) -> list[str]:
    return [(entry.get(ENTRY_ID_ATTR) or '').strip() for entry in element]


def _bound_list_reasons(where: str, before: ET.Element, after: ET.Element, delta: DefinitionDelta) -> list[str]:
    """A reference-bound list present before and after: existing entries stay unchanged and in order, an entry goes only
    if ``delta`` may remove it, and entries are appended only if ``delta`` creates them; Count and Ids must be exact."""
    if signature(before) == signature(after):
        return []
    entry_tag = REFERENCE_BOUND_LISTS[before.tag]
    for side, element in (('before', before), ('after', after)):
        problem = _bound_entries_problem(element, entry_tag)
        if problem:
            return [f'{where} malformed {side} the edit: {problem}']
    reasons: list[str] = []
    old_attrs = {key: value for key, value in attrs(before) if key != LIST_COUNT_ATTR}
    new_attrs = {key: value for key, value in attrs(after) if key != LIST_COUNT_ATTR}
    if old_attrs != new_attrs:
        reasons.append(f'{where} attributes changed: {sorted(key for key in set(old_attrs) | set(new_attrs) if old_attrs.get(key) != new_attrs.get(key))}')
    old, new = list(before), list(after)
    lost: list[str] = []
    index = 0
    for entry in old:
        ident = (entry.get(ENTRY_ID_ATTR) or '').strip()
        if index < len(new) and (new[index].get(ENTRY_ID_ATTR) or '').strip() == ident:
            if signature(new[index]) != signature(entry):
                reasons.append(f'{where}[{index}] existing {entry.tag} changed')
            index += 1
        else:
            lost.append(ident)
    old_ids = set(_entry_ids(before))
    removable, created = delta.removable.get(before.tag, frozenset()), delta.created.get(before.tag, frozenset())
    not_removable = [ident for ident in lost if ident not in removable]
    if not_removable:
        reasons.append(f'{where} lost entries {not_removable} that this edit may not remove')
    not_created = [ident for ident in _entry_ids(after)[index:] if ident not in created or ident in old_ids]
    if not_created:
        reasons.append(f'{where} unbound entries {not_created}: this edit does not create them')
    return reasons


def _bound_list_gone_reasons(where: str, element: ET.Element, delta: DefinitionDelta) -> list[str]:
    """A whole reference-bound list may disappear only when this edit may remove every entry it held."""
    problem = _bound_entries_problem(element, REFERENCE_BOUND_LISTS[element.tag])
    if problem:
        return [f'{where} lost {element.tag}, malformed before the edit: {problem}']
    removable = delta.removable.get(element.tag, frozenset())
    kept = [ident for ident in _entry_ids(element) if ident not in removable]
    if kept:
        return [f'{where} lost {element.tag} with entries {kept} that this edit may not remove']
    return []


def _bound_list_new_reasons(where: str, parent: ET.Element, element: ET.Element, siblings: list[ET.Element], delta: DefinitionDelta) -> list[str]:
    """A new reference-bound list: only in MAPPINGTABLE at its MAPPINGTABLE_ORDER place, well-formed, and holding only
    entries this edit creates."""
    added = [element.tag]
    if parent.tag != MAPPINGTABLE_TAG:
        return [f'{where} gained children {added} (position): only MAPPINGTABLE holds definition lists']
    problem = _bound_entries_problem(element, REFERENCE_BOUND_LISTS[element.tag])
    if problem:
        return [f'{where} gained children {added} (malformed): {problem}']
    created = delta.created.get(element.tag, frozenset())
    ids = _entry_ids(element)
    if not ids or any(ident not in created for ident in ids):
        return [f'{where} gained children {added} (unbound): this edit does not create entries {[i for i in ids if i not in created] or ids}']
    order = [child.tag for child in siblings]
    if any(tag not in MAPPINGTABLE_ORDER for tag in order):
        return [f'{where} gained children {added} (position): MAPPINGTABLE holds a list whose place is unknown']
    ranks = [MAPPINGTABLE_ORDER.index(tag) for tag in order]
    if ranks != sorted(ranks) or len(set(ranks)) != len(ranks):
        return [f'{where} gained children {added} (position): not at its place in {list(MAPPINGTABLE_ORDER)}']
    return []


def _head_growth_reasons(before: ET.Element, after: ET.Element, path: str, allow: Mapping[str, Iterable[str]], out: list[str],
                         delta: DefinitionDelta) -> None:
    """HEAD may change only by appending new entries to a ``GROWABLE_HEAD_LISTS`` list, and a reference-bound list
    only by what ``delta`` names.

    Every existing element, list entries included, must keep its attributes,
    text and children exactly (``signature``); only a growable list may gain
    children, only at its end, only of its entry tag, and only its ``Count``
    may follow. No element loses children except a reference-bound list by
    ``delta.removable``.
    """
    if len(out) >= MAX_REASONS:
        return
    where = f'{path}/{before.tag}'
    if after.tag != before.tag:
        out.append(f'{where} became {after.tag}')
        return
    if before.tag in REFERENCE_BOUND_LISTS:
        out.extend(_bound_list_reasons(where, before, after, delta))
        return
    old_children, new_children = list(before), list(after)
    entry_tag = GROWABLE_HEAD_LISTS.get(before.tag)
    if entry_tag and old_children and len({child.tag for child in old_children}) == 1:
        # The list's own entries name the entry element; the table above is only the fallback for an empty list.
        entry_tag = old_children[0].tag
    appended = new_children[len(old_children):] if entry_tag else []
    free = set(allow.get(before.tag, ())) | ({LIST_COUNT_ATTR} if appended else set())
    old_attrs = {key: value for key, value in attrs(before) if key not in free}
    new_attrs = {key: value for key, value in attrs(after) if key not in free}
    if before.tag in VIEW_STATE_HEAD_TAGS:
        old_attrs = new_attrs = {}
    if old_attrs != new_attrs:
        changed = sorted(key for key in set(old_attrs) | set(new_attrs) if old_attrs.get(key) != new_attrs.get(key))
        out.append(f'{where} attributes changed: {changed}')
    if (before.text or '').strip() != (after.text or '').strip():
        out.append(f'{where} text changed')
    if entry_tag:
        if len(new_children) < len(old_children):
            out.append(f'{where} lost {len(old_children) - len(new_children)} entries')
            return
        if any(child.tag != entry_tag for child in appended):
            out.append(f'{where} gained entries other than {entry_tag}')
        if appended:
            out.extend(_list_growth_reasons(where, before, after, len(old_children), appended))
        for index, (old, new) in enumerate(zip(old_children, new_children)):
            if signature(old) != signature(new):
                out.append(f'{where}[{index}] existing {old.tag} changed')
        return
    # Any other element keeps its children in order. Only a reference-bound list, at most one of its kind, may appear
    # (_bound_list_new_reasons) or disappear (_bound_list_gone_reasons).
    old_count, new_count = Counter(child.tag for child in old_children), Counter(child.tag for child in new_children)
    pairs: list[tuple[ET.Element, ET.Element]] = []
    i = j = 0
    while i < len(old_children) or j < len(new_children):
        old = old_children[i] if i < len(old_children) else None
        new = new_children[j] if j < len(new_children) else None
        if old is not None and new is not None and old.tag == new.tag:
            pairs.append((old, new))
            i, j = i + 1, j + 1
        elif new is not None and new.tag in REFERENCE_BOUND_LISTS and new_count[new.tag] == 1 and not old_count[new.tag]:
            out.extend(_bound_list_new_reasons(where, before, new, new_children, delta))
            j += 1
        elif old is not None and old.tag in REFERENCE_BOUND_LISTS and old_count[old.tag] == 1 and not new_count[old.tag]:
            out.extend(_bound_list_gone_reasons(where, old, delta))
            i += 1
        else:
            added = sorted((new_count - old_count).elements())
            removed = sorted((old_count - new_count).elements())
            if not removed and [child.tag for child in new_children if child.tag in old_count] != [child.tag for child in old_children]:
                out.append(f'{where} gained children {added} (misaligned): its existing children are not in their order')
            elif added:
                out.append(f'{where} gained children {added}, but only {sorted(GROWABLE_HEAD_LISTS)} may gain entries '
                           f'and only a section edit may add {sorted(REFERENCE_BOUND_LISTS)}')
            else:
                out.append(f'{where} lost children {removed}')
            return
    for index, (old, new) in enumerate(pairs):
        _head_growth_reasons(old, new, f'{where}[{index}]', allow, out, delta)


# HWPML2X reference attributes, as read back on Hancom 2024 (tests/fixtures/native_ordinary): a SECDEF names its outline
# numbering with OutlineShape and its memo shape with MemoShapeId (left out when 0); a PARASHAPE with HeadingType
# "Number" names its numbering with Heading.
SECDEF_REFERENCES: dict[str, str] = {'OutlineShape': 'NUMBERINGLIST', 'MemoShapeId': 'MEMOSHAPELIST'}
# HeadingType values whose Heading names no NUMBERING (None, and Bullet, which names a BULLET). Every other value counts
# as a NUMBERING reference, so an unknown heading type can only keep a definition, never let it go.
_NOT_NUMBERING_HEADINGS = frozenset({'none', 'bullet', '0', '3'})


# Memo fields, as read back on Hancom 2024 (tests/fixtures/native_memo): <FIELDBEGIN Type="Memo" Command="MEMO/<memo
# shape>/<number>/<date>/<time>/<author>/...">; the saved HWPX keeps the same value as the MemoShapeIDRef parameter.
MEMO_FIELD_TYPE = 'Memo'
_MEMO_COMMAND = re.compile(r'MEMO/([0-9]+)/')
# The memo shape value of a memo field that names no memo shape. Every memo Hancom 2024 inserted or saved read 65535:
# in a single-section document without memo shapes, and in either section of a two-section document whose second
# section has memo shape 1, after saving and reopening as well. No other sentinel was observed, so any other value
# must name an existing memo shape.
MEMO_SHAPE_NONE = frozenset({'65535'})


def _memo_field_reference(element: ET.Element, who: str, memo_ids: set[str] | None) -> tuple[str | None, str | None]:
    """(memo shape Id the field names, or None) and the problem when its reference cannot be read exactly."""
    field_type, command = element.get('Type'), element.get('Command')
    if field_type != MEMO_FIELD_TYPE:
        return None, f'{who} is a MEMO command on a field of type {field_type!r}'
    match = _MEMO_COMMAND.match(command or '')
    if match is None:
        return None, f'{who} of a memo field is {"missing" if command is None else "not MEMO/<memo shape>/..."}'
    value = match.group(1)
    if value in MEMO_SHAPE_NONE:
        return None, None
    if memo_ids is None or value not in memo_ids:
        return None, f'{who} names memo shape {value}, which the memo shape list does not hold'
    return value, None


def _is_memo_field(element: ET.Element) -> bool:
    return (element.get('Type') or '').lower() == 'memo' or (element.get('Command') or '').startswith('MEMO/')


def _memo_ids(root: ET.Element) -> set[str] | None:
    index, problem = _bound_index(root, 'MEMOSHAPELIST')
    return None if problem else set(index)


def memo_field_problems(root: ET.Element) -> list[str]:
    """Why a memo field's memo-shape reference cannot be read exactly (empty when every one can). While any remains,
    no memo shape may be treated as unreferenced."""
    memo_ids = _memo_ids(root)
    problems = []
    fields = (element for element in root.iter('FIELDBEGIN') if _is_memo_field(element))
    for index, element in enumerate(fields):
        _value, problem = _memo_field_reference(element, f'FIELDBEGIN[{index}].Command', memo_ids)
        if problem:
            problems.append(problem)
    return problems


def definition_references(root: ET.Element) -> dict[str, dict[str, list[str]]]:
    """Every readable reference to a NUMBERINGLIST or MEMOSHAPELIST entry, in BODY and HEAD: list tag -> entry Id ->
    referrers in document order.

    Referrers are ``SECDEF[n].<attribute>`` (n counts SECDEF elements in BODY order), ``PARASHAPE[Id=i].Heading``,
    ``FIELDBEGIN[n].Command`` for a memo field naming a memo shape (n counts memo fields), or ``<TAG>.<attribute>``
    for any other attribute whose name contains ``MemoShape``. A memo field whose reference cannot be read is not a
    reference here; memo_field_problems reports it, and callers must then keep every memo shape.
    """
    refs: dict[str, dict[str, list[str]]] = {tag: {} for tag in REFERENCE_BOUND_LISTS}

    def add(list_tag: str, value: str | None, who: str) -> None:
        if value is not None:
            refs[list_tag].setdefault(value.strip(), []).append(who)

    memo_ids = _memo_ids(root)
    secdef_index = memo_index = 0
    for element in root.iter():
        tag = element.tag if isinstance(element.tag, str) else ''
        if tag == 'FIELDBEGIN' and _is_memo_field(element):
            who = f'FIELDBEGIN[{memo_index}].Command'
            memo_index += 1
            add('MEMOSHAPELIST', _memo_field_reference(element, who, memo_ids)[0], who)
        if tag == 'SECDEF':
            for attr, list_tag in SECDEF_REFERENCES.items():
                add(list_tag, element.get(attr), f'SECDEF[{secdef_index}].{attr}')
            secdef_index += 1
            continue
        if tag == 'PARASHAPE' and (element.get('HeadingType') or '').strip().lower() not in _NOT_NUMBERING_HEADINGS:
            add('NUMBERINGLIST', element.get('Heading'), f'PARASHAPE[Id={element.get(ENTRY_ID_ATTR)}].Heading')
        for attr, value in element.attrib.items():
            if 'memoshape' in attr.lower():
                add('MEMOSHAPELIST', value, f'{tag}.{attr}')
    return refs


# Hancom 2024's default memo shape. When a break split a section without a memo shape (no MemoShapeId), the new
# section's SECDEF named a MEMO created with it whose attributes, besides its Id, were exactly these: the live readback's
# entry signature digest (head_summary) matches them and no other encoding of the saved memoPr (width 15591, SOLID line
# of width 1 in #B6D7AE, fill #F0FFE9, active #CFF1C7, NOMAL type; colours read back as BGR integers).
DEFAULT_MEMO_SHAPE: dict[str, str] = {'ActiveColor': '13103567', 'FillColor': '15335408', 'LineColor': '11458486', 'LineType': '1',
                                      'LineWidth': '1', 'MemoType': '0', 'Width': '15591'}
# Fixed problem names of a section definition reference (reason code section_ref_<attribute>_<problem>).
SECTION_REF_PROBLEMS = ('dropped', 'substituted', 'dangling', 'shared', 'unresolved', 'not_equivalent', 'not_default')


def _bound_index(root: ET.Element, list_tag: str) -> tuple[dict[str, ET.Element], str | None]:
    """Entry Id -> entry of the MAPPINGTABLE list ``list_tag`` (empty when absent), or a problem when it is not exact."""
    head = root.find(HEAD_TAG) if root.tag != HEAD_TAG else root
    mapping = head.find(MAPPINGTABLE_TAG) if head is not None else None
    found = [child for child in (list(mapping) if mapping is not None else []) if child.tag == list_tag]
    if len(found) > 1:
        return {}, f'{list_tag} appears {len(found)} times'
    if not found:
        return {}, None
    problem = _bound_entries_problem(found[0], REFERENCE_BOUND_LISTS[list_tag])
    if problem:
        return {}, problem
    return {(entry.get(ENTRY_ID_ATTR) or '').strip(): entry for entry in found[0]}, None


def _without_id(entry: ET.Element) -> str:
    return signature(entry, {entry.tag: frozenset({ENTRY_ID_ATTR})})


def section_insert_definitions(before_root: ET.Element, after_root: ET.Element, split: ET.Element, new: ET.Element,
                               new_index: int) -> tuple[DefinitionDelta, list[str], frozenset[str]]:
    """Bind the new SECDEF's HEAD references (SECDEF_REFERENCES) to the definitions of the section it was split from.

    Each reference must equal the split SECDEF's, or name an entry this edit created (absent before, present after,
    referenced by nothing but the new SECDEF) that equals, apart from its Id, the entry the split SECDEF names through
    the same attribute. A split section without a memo shape uses Hancom's default one (DEFAULT_MEMO_SHAPE). Returns the
    DefinitionDelta for head_tail_reasons, the reasons a reference is not bound, and the attributes bound this way.
    """
    created: dict[str, set[str]] = {}
    reasons: list[str] = []
    bound: set[str] = set()
    after_refs = definition_references(after_root)
    for attr, list_tag in SECDEF_REFERENCES.items():
        old, value = split.get(attr), new.get(attr)
        if value == old:
            continue

        def refuse(problem: str) -> None:
            reasons.append(f'section definition reference {attr} {problem}: the new section names {value!r}, the split section {old!r}')

        if value is None:
            refuse('dropped')
            continue
        before_index, before_problem = _bound_index(before_root, list_tag)
        after_index, after_problem = _bound_index(after_root, list_tag)
        if before_problem or after_problem:
            refuse('unresolved')
            continue
        ident = value.strip()
        if ident in before_index:
            refuse('substituted')
            continue
        entry = after_index.get(ident)
        if entry is None:
            refuse('dangling')
            continue
        if after_refs[list_tag].get(ident) != [f'SECDEF[{new_index}].{attr}']:
            refuse('shared')
            continue
        if old is None:
            if attr != 'MemoShapeId':
                refuse('unresolved')
                continue
            own = {key: val for key, val in entry.attrib.items() if key != ENTRY_ID_ATTR}
            if own != DEFAULT_MEMO_SHAPE or len(entry) or (entry.text or '').strip():
                refuse('not_default')
                continue
        else:
            original = before_index.get(old.strip())
            if original is None:
                refuse('unresolved')
                continue
            if _without_id(entry) != _without_id(original):
                refuse('not_equivalent')
                continue
        created.setdefault(list_tag, set()).add(ident)
        bound.add(attr)
    return DefinitionDelta(created={tag: frozenset(ids) for tag, ids in created.items()}), reasons, frozenset(bound)


def section_delete_definitions(before_root: ET.Element, after_root: ET.Element, removed: ET.Element, removed_index: int) -> DefinitionDelta:
    """The entries a section delete may remove: those the removed SECDEF (index ``removed_index`` in BODY order)
    references and nothing else references, neither in the document before (other SECDEFs, numbered paragraph shapes,
    memo fields, memo shape attributes anywhere) nor after the edit. While a memo field's reference cannot be read,
    before or after, no memo shape may be removed."""
    before_refs, after_refs = definition_references(before_root), definition_references(after_root)
    unreadable_memo = bool(memo_field_problems(before_root) or memo_field_problems(after_root))
    removable: dict[str, set[str]] = {}
    for attr, list_tag in SECDEF_REFERENCES.items():
        value = removed.get(attr)
        if value is None or (list_tag == 'MEMOSHAPELIST' and unreadable_memo):
            continue
        ident = value.strip()
        if before_refs[list_tag].get(ident) == [f'SECDEF[{removed_index}].{attr}'] and not after_refs[list_tag].get(ident):
            removable.setdefault(list_tag, set()).add(ident)
    return DefinitionDelta(removable={tag: frozenset(ids) for tag, ids in removable.items()})


def head_summary(root: ET.Element) -> dict[str, Any]:
    """Structure of the HEAD definition lists and of every SECDEF, for responses: element names, Count/Id values, entry
    signature hashes (Id excluded), SECDEF attribute values, the attributes of NUMBERING and MEMOSHAPE entries and what
    references them; never document text."""
    head = root if root.tag == HEAD_TAG else root.find(HEAD_TAG)
    mapping = head.find('MAPPINGTABLE') if head is not None else None
    lists: dict[str, Any] = {}
    for child in list(mapping) if mapping is not None else []:
        entries = list(child)
        lists[child.tag] = {
            'count': child.get(LIST_COUNT_ATTR), 'entries': len(entries), 'entry_tags': sorted({entry.tag for entry in entries}),
            'ids': [entry.get(ENTRY_ID_ATTR) for entry in entries[:64]],
            'signatures': [hashlib.sha256(signature(entry, {entry.tag: frozenset({ENTRY_ID_ATTR})}).encode('utf-8')).hexdigest()[:12] for entry in entries[:64]],
        }
        if child.tag in REFERENCE_BOUND_LISTS:
            lists[child.tag]['entry_attrs'] = [' '.join(f'{key}={value}' for key, value in sorted(entry.attrib.items())) for entry in entries[:16]]
    body = root.find('.//BODY') if root.tag != 'BODY' else root
    secdefs = [dict(sorted(element.attrib.items())) for element in (body.iter('SECDEF') if body is not None else [])]
    references = {tag: {key: who[:8] for key, who in list(found.items())[:16]} for tag, found in definition_references(root).items()}
    return {'head_children': [child.tag for child in head] if head is not None else [], 'mapping_lists': lists, 'secdefs': secdefs[:16],
            'references': references}


# Fixed diagnostic labels for a HEAD/TAIL reason: the deepest HWPML 2.x HEAD element the reason names, from this
# closed list of schema element names (never document text). Anything else is head_changed.
KNOWN_HEAD_TAGS = ('DOCSUMMARY', 'TITLE', 'SUBJECT', 'AUTHOR', 'DATE', 'KEYWORDS', 'COMMENTS', 'FORBIDDENSTRING', 'FORBIDDEN',
                   'DOCSETTING', 'BEGINNUMBER', 'CARETPOS', 'MAPPINGTABLE', 'BINDATALIST', 'BINITEM', 'FACENAMELIST', 'FONTFACE',
                   'FONT', 'SUBSTFONT', 'TYPEINFO', 'BORDERFILLLIST', 'BORDERFILL', 'CHARSHAPELIST', 'CHARSHAPE', 'TABDEFLIST',
                   'TABDEF', 'NUMBERINGLIST', 'NUMBERING', 'BULLETLIST', 'BULLET', 'PARASHAPELIST', 'PARASHAPE', 'STYLELIST', 'STYLE',
                   'MEMOSHAPELIST', 'MEMOSHAPE', 'MEMO', 'COMPATIBLEDOCUMENT', 'LAYOUTCOMPATIBILITY', 'TRACKCHANGELIST',
                   'TRACKCHANGEAUTHORLIST', 'HEAD')
_HEAD_CHANGE_KINDS = (('became', ' became '), ('attrs', ' attributes changed'), ('text', ' text changed'), ('lost', ' lost '),
                      ('gained', ' gained children'), ('foreign', ' gained entries other than'), ('ids', ' appended '),
                      ('entry', ' existing '), ('unbound', ' unbound '), ('malformed', ' malformed '))
# Why a new list was refused (head_<element>_newlist_<problem>).
_NEWLIST_PROBLEMS = ('misaligned', 'entries', 'position', 'malformed', 'unbound')
HEAD_REASON_CODES = frozenset({f'head_{tag.lower()}_{kind}' for tag in KNOWN_HEAD_TAGS for kind, _needle in _HEAD_CHANGE_KINDS}
                              | {f'head_{tag.lower()}_gained_{child.lower()}' for tag in KNOWN_HEAD_TAGS for child in (*KNOWN_HEAD_TAGS, 'OTHER')}
                              | {f'head_{tag.lower()}_newlist_{why}' for tag in KNOWN_HEAD_TAGS for why in _NEWLIST_PROBLEMS}
                              | {'head_changed', 'tail_changed', 'hwpml_parts_changed'})


def head_reason_code(reason: str) -> str | None:
    """The fixed label of a head_tail_reasons() reason, or None for any other reason.

    head_<element>_<kind>: the deepest changed HEAD element and how it changed (became, attrs, text, lost, gained,
    foreign, ids, entry, unbound, malformed); a gained child list is named too (head_<element>_gained_<child>), and a
    refused new definition list says why (head_<element>_newlist_<problem>). Names come only from the closed
    KNOWN_HEAD_TAGS list.
    """
    if reason.startswith('existing HEAD entry changed: '):
        path, _, rest = reason[len('existing HEAD entry changed: '):].partition(' ')
        tags = re.findall(r'/([A-Z][A-Z0-9]*)', path)
        if not tags or tags[-1] not in KNOWN_HEAD_TAGS:
            return 'head_changed'
        kind = next((name for name, needle in _HEAD_CHANGE_KINDS if needle in ' ' + rest), None)
        if kind is None:
            return 'head_changed'
        problem = re.search(r' \((' + '|'.join(_NEWLIST_PROBLEMS) + r')\)', rest) if kind == 'gained' else None
        if problem:
            return f'head_{tags[-1].lower()}_newlist_{problem.group(1)}'
        if kind == 'gained':
            added = re.findall(r"'([A-Z][A-Z0-9]*)'", rest.split(', but only', 1)[0])
            child = added[0] if added and added[0] in KNOWN_HEAD_TAGS else 'OTHER'
            return f'head_{tags[-1].lower()}_gained_{child.lower()}'
        return f'head_{tags[-1].lower()}_{kind}'
    if 'changed (it holds embedded binary data' in reason:
        return 'tail_changed'
    if reason.startswith('top-level HWPML parts changed'):
        return 'hwpml_parts_changed'
    return None


def head_tail_reasons(before_root: ET.Element, after_root: ET.Element, *, allow_head_attrs: Mapping[str, Iterable[str]] | None = None,
                      definitions: DefinitionDelta | None = None) -> list[str]:
    """Reasons HEAD did not only grow, or anything else outside BODY changed.

    ``allow_head_attrs`` names HEAD attributes an edit may legitimately change
    (tag -> attribute names), e.g. a section count after adding a section.
    ``definitions`` names the reference-bound entries a section edit creates or
    may remove; without it those lists may not change at all.
    """
    reasons: list[str] = []
    before_parts = [child for child in before_root if child.tag != BODY_TAG]
    after_parts = [child for child in after_root if child.tag != BODY_TAG]
    if [child.tag for child in before_parts] != [child.tag for child in after_parts]:
        return [f'top-level HWPML parts changed: {[c.tag for c in before_parts]} -> {[c.tag for c in after_parts]}']
    for old, new in zip(before_parts, after_parts):
        if old.tag == HEAD_TAG:
            found: list[str] = []
            _head_growth_reasons(old, new, '', allow_head_attrs or {}, found, definitions or DefinitionDelta())
            reasons.extend(f'existing HEAD entry changed: {item}' for item in found)
        elif signature(old) != signature(new):
            reasons.append(f'{old.tag} changed (it holds embedded binary data and document-level parts that must not change)')
    return reasons[:MAX_REASONS]
