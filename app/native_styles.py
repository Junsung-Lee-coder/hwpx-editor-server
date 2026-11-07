"""Named styles: an inventory of the document's own styles, applying one by name, creating one and changing one.

The document's HWPML2X text is the source and the proof. Its ``STYLELIST`` holds one ``STYLE`` per style, numbered by
``Id`` in list order: that number is the index a paragraph names in ``P/@Style`` and the index ``HStyle.Apply`` takes. A
style has a ``Type`` (``Para`` or ``Char``), a ``Name``, an optional ``EngName``, a ``NextStyle`` index, ``LockForm``,
``LangId``, a ``CharShape`` and, for a paragraph style, a ``ParaShape``. Shapes are compared by their resolved content,
never by number: Hancom renumbers character shapes (in the captured pair tests/fixtures/native_paragraph_edit/page_*
the 바탕글 style's CharShape goes from 0 to 1 with the same content). A paragraph style's use is the set of paragraphs
naming it, in the body, table cells, other nested lists and headers and footers. A run names only its CharShape in every
capture in this repository, so the text records no use of a character style and none is reported.

Official references (ActionTable_2504, ParameterSetTable_2504 set 109, ParameterSetObject HStyle, HwpAutomation_2504) and
Hancom's own answers on its developer forum (forum.developer.hancom.com, retrieved 2026-11-06 with their dates):
- Apply: ``Style`` with ``HParameterSet.HStyle``, whose ``Apply`` is the style index; ``HAction.GetDefault`` and
  ``Execute`` are BOOL and only True is a success. The ActionTable labels ``Style`` "스타일 (글2005이하버전)", for Hangul 2005
  and earlier, and lists ``StyleEx`` "스타일 (글2007)" with the same set. This module uses ``Style``, as the pinned pyhwpx
  1.7.2 ``set_style`` does (GetDefault and Execute of "Style" with HParameterSet.HStyle, Apply set in between), and does
  not switch to StyleEx without a measurement. The target is a block, as for the formatting commands (native_format): a
  paragraph style over the text of the paragraph(s), a character style over the exact range.
- Create: ``StyleAddNew`` through the legacy action family, as Hancom's answer in topic 2505 post 2 (2025-05-20) writes
  it: ``CreateAction("StyleAddNew")``, ``action.CreateSet()``, ``action.GetDefault(set)``, ``set.SetItem`` of NameLocal,
  NameEng and Next, the CharShape sub-set from ``ItemExist``/``Item`` with the requested items, ``set.SetItem("CharShape",
  sub-set)`` and ``action.Execute(set)``. The answer calls it outside the manual; the ActionTable does not list it.
- Change: ``StyleDirectEdit`` through the same family, as Hancom's answer in topic 1552 post 4 (2024-06-11) writes it for
  Hangul 2018 and later: ``CreateAction("StyleDirectEdit")``, ``CreateSet()``, ``GetDefault(set)``, ``SetItem("NameLocal",
  <the style to change>)``, the CharShape sub-set with the requested items, ``SetItem("CharShape", sub-set)`` and
  ``Execute(set)``. NameLocal selects the style: it is never written as a new name. That example makes the sub-set with
  ``CreateItemSet("CharShape", "CharShape")``. Hancom's answer in topic 1044 post 6 (2023-11-09, on a PageBorder FillAttr
  sub-set) says a sub-set made with CreateItemSet needs every one of its child items set, and a partial change edits the
  sub-set GetDefault filled, which ``Item`` gives, instead. C03 on Source08 held after this command's Execute answered True,
  with its sub-set made by CreateItemSet and only Height set: its proof found 바탕글's character shape other than the old one
  at 11 pt, and kept no text or model to say which items differed. Source09's change edited the filled sub-set from
  ``ItemExist`` and ``Item`` instead; C06 on Source09 was refused before the edit, because, by the source path,
  ``ItemExist("CharShape")`` answered False after GetDefault and the NameLocal SetItem: StyleDirectEdit's defaults held no
  CharShape sub-set on that host for 바탕글, for a reason not known. The change now writes the style's whole character shape
  read as ``IHwpObject.CharShape`` (HwpAutomation_2504 page 12: the selection's character shape, or the caret's without one,
  as a CharShape parameter set) with the caret in the style's plain body use and nothing selected, before StyleDirectEdit is
  made (an action is not to be made while another awaits its Execute, the official warning topic 2354 post 2 quotes). Every
  documented item must be there and every item the document text tells must be the style's own (_style_char_shape); only the
  requested items are written into it, and it is set back with ``SetItem("CharShape", set)``. StyleItem, StyleDirectEdit's
  set (ParameterSetTable_2504 set 133), holds CharShape as a set of that kind.
- Sub-sets are copies: Hancom's answer in topic 2354 post 5 (2025-03-27) says that under Hangul Automation a sub-set Item or
  CreateItemSet gives is a copy, not a reference as in HwpCtrl, so the filled sub-set is set on its parent. Topic 2505's
  example does this; topic 1552's omits it. Both commands set their CharShape set back once, before Execute: the create the
  sub-set its defaults filled, the change the whole shape read at the style's use.
HwpAutomation_2504 pages 61-64 declare that family's GetDefault, Execute and SetItem void, ItemExist BOOL, and CreateSet,
CreateItemSet and Item as returning a parameter set (Item answers 0 or an empty string for an item that does not exist).
The type library installed with Hancom 13.0.0.3457 (HwpObject.tlb, sha256 047f64bd…, 499852 bytes) declares
IDHwpAction.GetDefault VT_I4, Execute VT_BOOL and CreateSet a dispatch object, and IDHwpParameterSet.SetItem VT_VOID,
ItemExist VT_BOOL, Item a VARIANT and CreateItemSet a dispatch object; this module checks the installed declarations.
Each answer is checked before the next native call, or the edit holds at once with a fixed site label: GetDefault is the
exact builtin int its action's GetDefault answered natively (_GET_DEFAULT_OBSERVED: 1 for both, measured without an edit),
SetItem None, ItemExist a BOOL, a set an object, and Execute exactly True, the answer the same IDHwpAction.Execute gave
natively for this repository's InsertText, CharShape and ParagraphShape commands. The GetDefault 1 is the token of the
call, not a success: an edit succeeds only by Execute's True and the proof. The two families are never mixed: create and
change read no HAction and no HParameterSet, and pass no HSet (topic 1650 post 2, 2024-07-18: HStyleItem cannot be
handled from outside by any API).
StyleAdd with HStyleItem is no longer issued. An older answer (topic 821 post 2, 2023-07-21) said adding a user style was
not offered through the API; the 2024 and 2025 answers above name these two actions.

What these examples show is what this module issues. A character style is not created and no style is renamed; paragraph
properties of a new or changed style are not written; each of these is refused before any native style action, caret move
or write (the document text is read first, as for every command). For both
actions the caret stands in a body paragraph that uses the base style (create) or the style itself (change) and shows
exactly its definitions, so whatever GetDefault fills the set with from the caret is those definitions, and only the
requested items are written over them. A create whose defaults hold no CharShape sub-set is refused when it requests a
character property: a sub-set made empty would not carry the other character properties. A change whose read shape lacks a
documented item, or is not the style's own where the document text tells, is refused as style_shape_unverified. Which paragraphs a
changed style reaches natively is not measured: a change is refused unless every use shows exactly the style's
definitions, so its outcome is one expected state. A next style is a paragraph style: the next paragraph takes it.

The native boundary. Until the edit first places the caret or selects a block, a refusal changes nothing, and only the
document text is read again to prove it. From then on, a caret, selection, GetDefault, parameter or select value that is
not of its documented type, and any native call that raises, is an unknown outcome at once
(NativeNumberingReadFailure): no further native call follows, not even giving the caret back. The parameter set's HSet,
which GetDefault takes, is read first as an object (_defaults): an HSet that raises, is None or a plain value, or is
absent is an unknown outcome at once, and GetDefault is not called. A well-typed answer that refuses (GetDefault False,
select False, the caret or block well-typed but elsewhere) stays a clean refusal, with the caret and selection given back.

Before that point the same holds for every native read the edit makes. The document text is read as a document
(document_text): a reply that is not a str, is empty or is not well-formed XML is an unknown outcome at once, at the
service's first read, the engine's read and the inventory alike. A well-formed document the style model cannot represent
is the clean refusal style_model_unusable. The capabilities (for apply HAction with GetDefault and Execute, HParameterSet
and its HStyle; for create and change CreateAction, the action with its CreateSet, GetDefault and Execute, and the set with
the methods the command calls; for a change also IHwpObject.CharShape with its ItemExist, Item and SetItem) and the methods the edit calls (set_pos, and MoveDocBegin and find for a table cell) are read
through the wrapper, whose HAction and HParameterSet are getters into the COM object (pinned pyhwpx 1.7.2 core.py
1248-1268 and 1302-1319); the COM object's own members are dynamic. A read that raises, returns None or a plain value
for an object, or returns something that cannot be called for a method, is an unknown outcome at once (_member). Only an
attribute that is absent without running any code but Python's own lookup (no attribute, descriptor or class member by
that name, and no __getattr__ or __getattribute__ of the object's classes; inspect.getattr_static, which triggers
neither) is the clean refusal native_unavailable: an AttributeError from a dynamic or declared member is not absence. A
change reads IHwpObject.CharShape and then its legacy family after the caret has moved, so there even such an absence
holds at its site, as for every member read once the edit has entered native navigation (_entered_member).

A held edit carries two fixed labels into its public failure_diagnostic (app.style_rules.NATIVE_SITES and NATIVE_RETURNS):
the site it stopped at (the document text, a caret step, or one call of the legacy family) and the exact builtin shape of
the answer there (none, true, int, str, raised, absent and the like). Never a value, a message, a type name or anything the
answer defines; no native call is made to find them. The hold is no new authority to close or retry.

After the action the whole document is compared with that expected state (see _problems): every paragraph anywhere,
by the style it names, paragraph shape, every character and control with the full content of its character shape (its
fonts included, TYPEINFO and all, not only their names) and the settings of the run and CHAR holding it, its end run and
its other settings; every style by its settings and resolved definitions, and the fonts a new or changed style names; the
header and footer frames, the census digests and the definition lists. Run settings are kept per character, so a run
split or merge with the same settings passes and a setting moved to other characters does not. No capture in this
repository has a TEXT setting besides CharShape, or any setting on an inline mark (TAB, LINEBREAK, NBSPACE, FWSPACE); an
inline mark with one makes the document unusable for this module rather than being read without it. Anything else is
an unknown outcome that is never rolled back.

Styles are compared by identity, not by number (_link). A style is its name and kind, which each edit first requires to be
unique, with every next style in the list (style_name_ambiguous, style_model_unusable). After the action each old style is
found again by its identity wherever it now is, and must have every setting but its NextStyle number, both definitions by
their content, and as next style the style of the same identity, one step and no further. A create adds exactly one style
of the requested name at whatever index; nothing is matched by appearance. The typed style references of HWPML2X,
P/@Style and STYLE/@NextStyle, are compared by the identity they resolve to in their own list, so an action that inserts
the new style before others and renumbers the rest is no change. The census digest of a header or footer embeds the style
element of each of its paragraphs, NextStyle number included, so it is compared while no style was renumbered; the same
header or footer without its paragraphs' style references always is. C02 on Source06 held at the index-by-index proof this
replaces: that proof read the document text once after the action and kept neither the text nor its model, and nothing further
was read after the hold. C04 on Source07 measured with this proof where StyleAddNew put the new style on one host, based on the
style at index 0: right after the base, with the set's Next resolving as an index of the list after the insertion. A create
plans its Next for that slot (_planned_next), and the proof still holds any other outcome.

A held outcome carries bounded facts (_not_as_requested, app.style_rules.style_proof_facts): which predicate failed for
which style, field or place, measured from the text already read before the edit holds, with style names, fixed labels,
indexes and counts and never document text; the message names the first problems the same way and counts the rest. A
character shape that is not as expected also says what of it differs (_character_facts): the size expected and found, the
differing items by fixed label and their count, and whether any was not requested.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
import re
from typing import Any, Callable, Mapping
import xml.etree.ElementTree as ET

from app import native_format as nf, style_rules
from app.native_numbering import (
    STEP_MATCH_MAX_CHARS,
    NativeNumberingInputError,
    NativeNumberingMutationError,
    NativeNumberingReadFailure,
    NativeNumberingRefusal,
    _STEP_ENVELOPE_KEYS,
    _MutationReach,
    _action_confirmed,
    _get_selection,
    _multiset_delta,
    _read_target_state,
    _reject_unknown_keys,
    _require_confirm_layout,
    _require_motion_result,
    _sdk_read,
    _status_text,
)
from app.native_numbering_census import (
    CensusError,
    MAX_HWPML_CHARS,
    _DOCTYPE,
    _INLINE_CHARACTERS,
    _MAX_DEPTH,
    _Resolver,
    _Unresolved,
    _attr,
    _lower_attrs,
    _lower_attrs_all,
    _name,
)
from app.native_paragraph_edit import _COMPARED, _exception_class_label

KINDS = style_rules.KINDS
_NATIVE_KINDS = {'para': 'paragraph', 'char': 'character'}
# The two style actions of the legacy action family this module issues (see the module docstring); never a caller's name.
_CREATE_ACTION = 'StyleAddNew'
_CHANGE_ACTION = 'StyleDirectEdit'
# What each action's GetDefault answered natively, the only answers accepted. The installed HwpObject.tlb (sha256 047f64bd…,
# 499852 bytes, Hwp 13.0.0.3457) declares IDHwpAction.GetDefault (dispid 15000) VT_I4. GD01, a fresh owned instance of
# Hwp 13.0.0.3457 on the native-authored seed, 2026-11-06T17:32:29Z: StyleAddNew.GetDefault answered the exact builtin int 1.
# GD02, a separate fresh owned instance, 18:00:23Z: StyleDirectEdit.GetDefault answered the exact builtin int 1. Both bound
# action IID {1BE3D304-747E-4702-B02A-3F23B1A6F53C} and set IID {599CBB08-7780-4F3B-8ADA-7F2ECFB57181}; neither edited a
# style. The 1 is the token of the call as observed, not a success: an edit succeeds only by Execute's True and the proof.
_GET_DEFAULT_OBSERVED = {_CREATE_ACTION: frozenset({1}), _CHANGE_ACTION: frozenset({1})}
CONTEXTS = ('body', 'table_cell', 'other', 'header', 'footer')
MAX_STYLES = 1000
USE_ROWS = 50
USE_ROWS_ONE = 1000
NAME_MAX_CHARS = style_rules.NAME_MAX_CHARS
USE_TEXT = 80
_HASH_HEX = 24
_HASH = re.compile(r'sha256:[0-9a-f]{24}')
_TARGET_FIELDS = ('match', 'through_match', 'paragraph_index', 'expected_text', 'expected_page')
_CHARACTER_FIELDS = ('font_family', 'size_pt', 'bold', 'italic', 'underline', 'text_color')
_PARAGRAPH_FIELDS = ('align', 'left_indent_pt', 'right_indent_pt', 'first_line_indent_pt', 'line_spacing_type', 'line_spacing_value')
_APPLY_FIELDS = ('style_name', 'expected_style_hash', *_TARGET_FIELDS)
_CREATE_FIELDS = ('name', 'english_name', 'kind', 'based_on', 'expected_based_on_hash', 'expected_styles_hash', 'next_style', *_CHARACTER_FIELDS, *_PARAGRAPH_FIELDS)
_UPDATE_FIELDS = ('style_name', 'expected_style_hash', 'new_name', 'new_english_name', 'expected_uses', 'expected_uses_hash', 'all_uses',
                  *_CHARACTER_FIELDS, *_PARAGRAPH_FIELDS)
# Lists a style edit may grow or empty: a new definition for the style, and one Hancom drops when nothing uses it any more.
_DEFINITION_LISTS = frozenset({'charshapelist', 'parashapelist', 'facenamelist'})
# The code of an unknown outcome at the native boundary: not a clean refusal code, so no caller can read it as one.
_UNPROVEN = 'native_value_unproven'


class StyleModelUnusable(CensusError):
    """The style list or a paragraph's style cannot be read as this module reads them."""


def document_text(source: Any) -> str:
    """A get_text_file('HWPML2X') reply as a document: a str that is well-formed XML. A reply that is not a str, is empty or is
    not well-formed is an unknown outcome at once. A text over the size bound or with a DOCTYPE is not parsed here; the
    style model refuses it, cleanly, as it refuses any well-formed document it cannot represent."""
    if type(source) is not str:
        raise _held('document.text', _shape(source), 'the native HWPML2X text is not text')
    if not source.strip():
        raise _held('document.text', 'empty', 'the native HWPML2X text is empty')
    if len(source) > MAX_HWPML_CHARS or _DOCTYPE.search(source):
        return source
    try:
        ET.fromstring(source)
    except ET.ParseError as exc:
        raise _held('document.text', 'not_well_formed', 'the native HWPML2X text is not well-formed XML') from exc
    return source


# Failure labels: fixed strings only (app.style_rules.NATIVE_SITES and NATIVE_RETURNS), for the public failure_diagnostic.

_SHAPES = ((int, 'int'), (float, 'float'), (complex, 'complex'), (str, 'str'), (bytes, 'bytes'), (bytearray, 'bytes'), (tuple, 'tuple'),
           (list, 'list'), (dict, 'dict'), (set, 'set'), (frozenset, 'set'))


def _shape(value: Any) -> str:
    """A fixed code for the exact builtin type of a native answer. Nothing the answer defines is called or read."""
    if value is None:
        return 'none'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    kind = type(value)
    for builtin, label in _SHAPES:
        if kind is builtin:
            return label
    return 'object'


def _labelled(error: NativeNumberingReadFailure, site: str | None, returned: str) -> NativeNumberingReadFailure:
    if site is not None:
        error.native_site, error.native_return = site, returned
    return error


def _held(site: str, returned: str, message: str) -> NativeNumberingReadFailure:
    """An unknown outcome at once, labelled with the fixed site it stopped at and the fixed shape of the answer there."""
    return _labelled(NativeNumberingReadFailure(f'{message}; nothing further is called', code=_UNPROVEN), site, returned)


# Requests.

def _style_name(value: Any, name: str) -> str:
    problem = style_rules.name_problem(value)
    if problem is not None:
        raise NativeNumberingInputError(f'{name}: {problem}')
    return value


def _hash(value: Any, name: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise NativeNumberingInputError(f'{name} must be the sha256:<24 hex> hash style_inventory reported')
    return value


def _properties(step: Mapping[str, Any], *, paragraph: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """The requested character and paragraph properties, checked by the formatting commands' own rules and limits."""
    character = {key: step[key] for key in _CHARACTER_FIELDS if step.get(key) is not None}
    para = {key: step[key] for key in _PARAGRAPH_FIELDS if step.get(key) is not None}
    if para and not paragraph:
        raise NativeNumberingInputError(f"a character style has no paragraph properties ({', '.join(sorted(para))})")
    wanted_character = nf.validate_character_step({'match': 'x', 'expected_page': 1, 'confirm_layout': True, **character})['properties'] if character else {}
    wanted_paragraph = nf.validate_paragraph_step({'match': 'x', 'expected_page': 1, 'confirm_layout': True, **para})['properties'] if para else {}
    return wanted_character, wanted_paragraph


def validate_inventory_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, ('name', *_STEP_ENVELOPE_KEYS), 'style_inventory step')
    return {'name': _style_name(step['name'], 'name')} if step.get('name') is not None else {}


def validate_apply_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_APPLY_FIELDS, *_STEP_ENVELOPE_KEYS), 'style_apply_exact step')
    _require_confirm_layout(step, 'style_apply_exact')
    name = _style_name(step.get('style_name'), 'style_name')
    expected = _hash(step.get('expected_style_hash'), 'expected_style_hash')
    target = nf._validated_target(step, 'style_apply_exact')  # noqa: SLF001
    return {**target, 'style_name': name, 'expected_style_hash': expected}


def validate_create_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_CREATE_FIELDS, *_STEP_ENVELOPE_KEYS), 'style_create_exact step')
    _require_confirm_layout(step, 'style_create_exact')
    kind = step.get('kind')
    if type(kind) is not str or kind not in KINDS:
        raise NativeNumberingInputError('kind must be paragraph or character')
    name = _style_name(step.get('name'), 'name')
    english = _style_name(step['english_name'], 'english_name') if step.get('english_name') is not None else name
    based_on = _style_name(step.get('based_on'), 'based_on')
    next_style = None
    if step.get('next_style') is not None:
        if kind != 'paragraph':
            raise NativeNumberingInputError('next_style is for a paragraph style only')
        next_style = _style_name(step['next_style'], 'next_style')
    character, paragraph = _properties(step, paragraph=kind == 'paragraph')
    return {'name': name, 'english_name': english, 'kind': kind, 'based_on': based_on,
            'expected_based_on_hash': _hash(step.get('expected_based_on_hash'), 'expected_based_on_hash'),
            'expected_styles_hash': _hash(step.get('expected_styles_hash'), 'expected_styles_hash'),
            'next_style': next_style, 'character': character, 'paragraph': paragraph}


def validate_update_step(step: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_keys(step, (*_UPDATE_FIELDS, *_STEP_ENVELOPE_KEYS), 'style_update_exact step')
    _require_confirm_layout(step, 'style_update_exact')
    name = _style_name(step.get('style_name'), 'style_name')
    expected = _hash(step.get('expected_style_hash'), 'expected_style_hash')
    uses = step.get('expected_uses')
    if type(uses) is not int or not 0 <= uses <= 1_000_000:
        raise NativeNumberingInputError('expected_uses must be the number of uses style_inventory reported for this style')
    uses_hash = _hash(step.get('expected_uses_hash'), 'expected_uses_hash')
    every = step.get('all_uses', False)
    if type(every) is not bool:
        raise NativeNumberingInputError('all_uses must be true or false')
    new_name = _style_name(step['new_name'], 'new_name') if step.get('new_name') is not None else None
    new_english = _style_name(step['new_english_name'], 'new_english_name') if step.get('new_english_name') is not None else None
    character, paragraph = _properties(step, paragraph=True)
    if not character and not paragraph and new_name is None and new_english is None:
        raise NativeNumberingInputError('style_update_exact needs at least one property, new_name or new_english_name')
    return {'style_name': name, 'expected_style_hash': expected, 'expected_uses': uses, 'expected_uses_hash': uses_hash, 'all_uses': every,
            'new_name': new_name, 'new_english_name': new_english, 'character': character, 'paragraph': paragraph}


# The document's styles and paragraphs.

def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8', 'surrogatepass')).hexdigest()


def _public_hash(value: Any) -> str:
    return 'sha256:' + _digest(value)[:_HASH_HEX]


def _integer(value: str | None, what: str) -> int:
    text = (value or '').strip()
    if not text.isdigit():
        raise StyleModelUnusable(f'{what} is not a number')
    return int(text)


def _numbering(heading: tuple[str, str, str] | None) -> dict[str, Any] | None:
    if heading is None or heading[0] not in {'outline', 'number', 'bullet'}:
        return None
    return {'kind': heading[0], 'level': int(heading[1]) + 1 if heading[1].isdigit() else None}


def _styles(root: ET.Element, resolver: _Resolver, chars: Mapping[str, Any], paras: Mapping[str, Any],
            headings: Mapping[str, tuple[str, str, str]]) -> list[dict[str, Any]]:
    lists = [element for element in root.iter() if _name(element) == 'stylelist']
    if len(lists) != 1:
        raise StyleModelUnusable('the document has no single style list')
    items = list(lists[0])
    if any(_name(item) != 'style' for item in items) or not items:
        raise StyleModelUnusable('the style list holds something other than styles')
    if len(items) > MAX_STYLES:
        raise StyleModelUnusable(f'the document has more than {MAX_STYLES} styles')
    styles = []
    for number, element in enumerate(items):
        attrs = _lower_attrs(element)
        if len(attrs) != len(element.attrib):
            raise StyleModelUnusable('a style repeats an attribute')
        if _integer(attrs.get('id'), 'a style id') != number:
            raise StyleModelUnusable('the style ids are not the list order')
        kind = _NATIVE_KINDS.get((attrs.get('type') or '').strip().lower())
        name = attrs.get('name')
        if kind is None or not name:
            raise StyleModelUnusable('a style has no known type or no name')
        char_ref, para_ref = (attrs.get('charshape') or '').strip(), (attrs.get('parashape') or '').strip()
        if char_ref not in chars or (kind == 'paragraph' and para_ref not in paras):
            raise StyleModelUnusable('a style names a shape that is not defined')
        try:
            char_digest = resolver.definition('charshapelist', char_ref)
            para_digest = resolver.definition('parashapelist', para_ref) if kind == 'paragraph' else None
            fonts = _fonts_of(resolver, resolver.lookup('charshapelist', char_ref))
        except _Unresolved as exc:
            raise StyleModelUnusable('a style definition does not resolve') from exc
        settings = sorted((key, value) for key, value in attrs.items() if key not in {'id', 'charshape', 'parashape'})
        styles.append({'index': number, 'kind': kind, 'name': name, 'english_name': attrs.get('engname'),
                       'next': _integer(attrs.get('nextstyle'), 'a next style'), 'lock': (attrs.get('lockform') or '').strip() == '1',
                       'settings': settings, 'char': chars[char_ref], 'para': paras[para_ref] if kind == 'paragraph' else None,
                       'char_sha256': char_digest, 'para_sha256': para_digest, 'char_fonts': fonts,
                       'numbering': _numbering(headings.get(para_ref)) if kind == 'paragraph' else None})
    for item in styles:
        item['sha256'] = _digest([item['index'], item['kind'], item['settings'], item['char_sha256'], item['para_sha256']])
        item['hash'] = 'sha256:' + item['sha256'][:_HASH_HEX]
    return styles


def _fonts_of(resolver: _Resolver, charshape: ET.Element) -> dict[str, str]:
    """The content digest of the font a character shape names for each language: the whole FONT, TYPEINFO included, without its Id."""
    found: dict[str, str] = {}
    for child in charshape:
        if _name(child) != 'fontid':
            continue
        for language, ref in _lower_attrs(child).items():
            digest = resolver.fonts.get((language, ref.strip()))
            if digest is None:
                raise StyleModelUnusable('a style names a font that is not listed')
            found[language] = digest
    return found


def _owners(element: ET.Element, resolver: _Resolver) -> list[tuple[str, tuple, tuple | None]]:
    """For every character and control of the paragraph, in the order native_format._atoms reads them: the content digest of its run's
    character shape (fonts resolved to their whole content), the run's settings besides CharShape, and the settings of the CHAR element
    holding it (None outside one). No capture has such run or CHAR settings; they are compared as they are, per character."""
    owners: list[tuple[str, tuple, tuple | None]] = []
    for run in element:
        if _name(run) != 'text':
            continue
        shape = resolver.definition('charshapelist', _attr(run, 'charshape') or '')
        settings = tuple(sorted((key, value) for key, value in _lower_attrs_all(run) if key != 'charshape'))
        for child in run:
            if _name(child) != 'char':
                owners.extend([(shape, settings, None)] * (1 + len(child.tail or '')))
                continue
            holder = tuple(sorted(_lower_attrs_all(child)))
            owners.extend([(shape, settings, holder)] * len(child.text or ''))
            for inner in child:
                if _name(inner) in _INLINE_CHARACTERS and inner.attrib:
                    raise StyleModelUnusable('an inline mark carries settings this module does not read')
                owners.extend([(shape, settings, holder)] * (1 + len(inner.tail or '')))
            owners.extend([(shape, settings, None)] * len(child.tail or ''))
    return owners


def _row(element: ET.Element, chars: Mapping[str, Any], paras: Mapping[str, Any], resolver: _Resolver, styles: list[dict[str, Any]]) -> dict[str, Any]:
    attrs = _lower_attrs_all(element)
    style = [value for key, value in attrs if key == 'style']
    shape = [value for key, value in attrs if key == 'parashape']
    if len(style) != 1 or len(shape) != 1:
        raise StyleModelUnusable('a paragraph has no single style or paragraph shape')
    index = _integer(style[0], 'a paragraph style')
    if index >= len(styles) or styles[index]['kind'] != 'paragraph':
        raise StyleModelUnusable('a paragraph names a style that is not a paragraph style of the list')
    para = paras.get(shape[0].strip())
    if para is None:
        raise StyleModelUnusable('a paragraph has no resolvable paragraph shape')
    runs = [child for child in element if _name(child) == 'text']
    try:
        atoms = nf._atoms(element, chars, resolver)  # noqa: SLF001
        owners = _owners(element, resolver)
        extra = [resolver.digest(child, prune=True) for child in element if _name(child) != 'text']
        para_sha = resolver.definition('parashapelist', shape[0])
        last = runs[-1] if runs else None
        end_sha = resolver.definition('charshapelist', _attr(last, 'charshape')) if last is not None and _attr(last, 'charshape') is not None else None
    except (CensusError, _Unresolved) as exc:
        raise StyleModelUnusable('a paragraph does not resolve to characters with known shapes') from exc
    if len(owners) != len(atoms):
        raise StyleModelUnusable("a paragraph's characters could not be told apart by run")
    # A run is where a shape starts and ends, which Hancom splits and merges as shapes change: each character keeps its own
    # shape's content, its run's settings and its CHAR's settings, so a split or merge is no change and a moved setting is one.
    for atom, (shape_sha, settings, holder) in zip(atoms, owners):
        atom.update(shape_sha=shape_sha, run=settings, holder=holder)
    end = next((chars.get((_attr(run, 'charshape') or '').strip()) for run in reversed(runs)), None)
    return {'style': index, 'para': para, 'para_sha': para_sha, 'atoms': atoms, 'end': end, 'end_sha': end_sha,
            'end_run': tuple(sorted((key, value) for key, value in _lower_attrs_all(last) if key != 'charshape')) if last is not None else None,
            'text': ''.join(atom['char'] for atom in atoms if 'char' in atom),
            'settings': sorted((key, value) for key, value in attrs if key not in {'style', 'parashape', 'instid'}),
            'inst': [value for key, value in attrs if key == 'instid'], 'extra': extra}


def _without_style_references(element: ET.Element) -> ET.Element:
    """A copy of a header or footer with every paragraph's style reference (P/@Style) left out: everything else of it, in full, for a digest
    that a renumbering of the style list does not change. The references themselves are compared by the identity they resolve to."""
    copied = deepcopy(element)
    for item in copied.iter():
        if _name(item) == 'p':
            for key in [key for key in item.attrib if key.rsplit('}', 1)[-1].lower() == 'style']:
                del item.attrib[key]
    return copied


def parse_style_model(text: str) -> dict[str, Any]:
    """The style list, and every paragraph of the document in document order with its context, style and contents."""
    if type(text) is not str or len(text) > MAX_HWPML_CHARS or _DOCTYPE.search(text):
        raise StyleModelUnusable('HWPML2X text is not usable as a document')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise StyleModelUnusable('HWPML2X text is not well-formed') from exc
    body = next((element for element in root.iter() if _name(element) == 'body'), None)
    if body is None:
        raise StyleModelUnusable('HWPML2X text has no BODY')
    resolver = _Resolver(root, body)
    try:
        chars, paras, _fonts, headings = nf._shape_tables(root, resolver)  # noqa: SLF001
    except CensusError as exc:
        raise StyleModelUnusable('the shape definitions are not usable') from exc
    styles = _styles(root, resolver, chars, paras, headings)
    rows: list[dict[str, Any]] = []
    areas: list[dict[str, Any]] = []
    census = [0]
    section = [-1]

    def visit(element: ET.Element, chain: tuple[str, ...], area: dict[str, Any] | None, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise StyleModelUnusable('HWPML2X nesting is too deep')
        for child in element:
            name = _name(child)
            if name == 'section':
                section[0] += 1
                visit(child, chain + (name,), area, depth + 1)
            elif name in {'header', 'footer'} and area is None:
                number = max(section[0], 0)
                scope = (_attr(child, 'applypagetype') or '').strip().lower()
                try:
                    shell = resolver.digest(child, prune=True)
                    content = resolver.digest(_without_style_references(child))
                except _Unresolved as exc:
                    raise StyleModelUnusable('a header or footer does not resolve') from exc
                found = {'kind': name, 'section': number, 'apply_to': scope, 'id': f'{name}:s{number}:{scope}', 'order': len(areas), 'count': 0,
                         'shell': shell, 'content': content}
                areas.append(found)
                visit(child, (name,), found, depth + 1)
            elif name == 'p':
                item = _row(child, chars, paras, resolver, styles)
                if area is not None:
                    item.update(context=area['kind'], census_index=None, area=area['id'], area_order=area['order'], area_paragraph=area['count'])
                    area['count'] += 1
                else:
                    context = 'body' if chain[-1] == 'section' else 'table_cell' if 'cell' in chain and set(chain) <= nf._SECTION_CELL_CHAIN else 'other'  # noqa: SLF001
                    item.update(context=context, census_index=census[0], area=None, area_order=None, area_paragraph=None)
                    census[0] += 1
                item['number'] = len(rows)
                rows.append(item)
                visit(child, chain + (name,), area, depth + 1)
            else:
                visit(child, chain + (name,), area, depth + 1)

    visit(body, ('body',), None, 0)
    for item in rows:
        definition = styles[item['style']]
        item['plain'] = (item['para_sha'] == definition['para_sha256'] and all(atom['shape_sha'] == definition['char_sha256'] for atom in item['atoms'])
                         and (item['end_sha'] is None or item['end_sha'] == definition['char_sha256']))
    # Every listed font by language and name, by content: what a requested font_family must be when the document already has it.
    fonts_by_name: dict[tuple[str, str], set[str]] = {}
    for face in resolver.items.get('facenamelist', []):
        language = (_attr(face, 'lang') or '').strip().lower()
        for font in face:
            font_id, font_name = _attr(font, 'id'), _attr(font, 'name')
            digest = resolver.fonts.get((language, font_id.strip())) if _name(font) == 'font' and font_id is not None else None
            if digest is not None and font_name is not None:
                fonts_by_name.setdefault((language, font_name), set()).add(digest)
    styles_hash = _public_hash([item['sha256'] for item in styles])
    return {'styles': styles, 'paragraphs': rows, 'areas': [{key: value for key, value in item.items() if key != 'count'} for item in areas],
            'styles_hash': styles_hash, 'fonts_by_name': fonts_by_name}


# The inventory.

def _uses(model: Mapping[str, Any], index: int) -> list[dict[str, Any]]:
    return [item for item in model['paragraphs'] if item['style'] == index]


def _public_use(item: Mapping[str, Any]) -> dict[str, Any]:
    return {'context': item['context'], 'paragraph_index': item['census_index'], 'area': item['area'], 'area_paragraph': item['area_paragraph'],
            'text': item['text'][:USE_TEXT], 'text_truncated': len(item['text']) > USE_TEXT, 'plain': item['plain']}


def _uses_hash(uses: list[Mapping[str, Any]]) -> str:
    return _public_hash([[item['context'], item['census_index'], item['area'], item['area_paragraph'],
                          hashlib.sha256(item['text'].encode('utf-8', 'surrogatepass')).hexdigest(), item['plain']] for item in uses])


def public_style(model: Mapping[str, Any], item: Mapping[str, Any], *, rows: int = USE_ROWS) -> dict[str, Any]:
    styles = model['styles']
    following = styles[item['next']] if item['next'] < len(styles) else None
    found: dict[str, Any] = {
        'index': item['index'], 'name': item['name'], 'english_name': item['english_name'], 'kind': item['kind'],
        'next_style': {'index': item['next'], 'name': following['name'] if following is not None else None},
        'lock_form': item['lock'],
        'character': nf.public_character(item['char']), 'character_rest': nf._char_remainder(item['char']),  # noqa: SLF001
        'paragraph': nf.public_paragraph(item['para']) if item['para'] is not None else None,
        'paragraph_rest': nf._para_remainder(item['para']) if item['para'] is not None else None,  # noqa: SLF001
        'numbering': item['numbering'],
        'hash': item['hash'],
    }
    if item['kind'] != 'paragraph':
        found['uses'] = {'recorded': False}
        return found
    uses = _uses(model, item['index'])
    found['uses'] = {'recorded': True, 'total': len(uses), 'plain': sum(1 for use in uses if use['plain']),
                     'by_context': {context: sum(1 for use in uses if use['context'] == context) for context in CONTEXTS},
                     'rows': [_public_use(use) for use in uses[:rows]], 'rows_complete': len(uses) <= rows, 'hash': _uses_hash(uses)}
    return found


def inventory(model: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    styles = model['styles']
    names = [item['name'] for item in styles]
    chosen = [item for item in styles if 'name' not in request or item['name'] == request['name']]
    return {'count': len(styles), 'paragraph_styles': sum(1 for item in styles if item['kind'] == 'paragraph'),
            'character_styles': sum(1 for item in styles if item['kind'] == 'character'), 'styles_hash': model['styles_hash'],
            'duplicate_names': sorted({name for name in names if names.count(name) > 1}),
            'styles': [public_style(model, item, rows=USE_ROWS_ONE if 'name' in request else USE_ROWS) for item in chosen]}


# Bindings.

def _find(model: Mapping[str, Any], name: str) -> dict[str, Any]:
    found = [item for item in model['styles'] if item['name'] == name]
    if not found:
        raise NativeNumberingRefusal('the document has no style of this name', code='style_not_found')
    if len(found) > 1:
        raise NativeNumberingRefusal(f'{len(found)} styles have this name, so which one is meant cannot be proven', code='style_name_ambiguous')
    return found[0]


def _bound(model: Mapping[str, Any], name: str, expected: str) -> dict[str, Any]:
    found = _find(model, name)
    if found['hash'] != expected:
        raise NativeNumberingRefusal('the style is not as style_inventory reported it; read the inventory again', code='style_hash_mismatch')
    return found


def _require_free(model: Mapping[str, Any], name: str, *, own: int | None = None) -> None:
    if any(item['name'] == name and item['index'] != own for item in model['styles']):
        raise NativeNumberingRefusal('another style already has this name', code='style_name_taken')


# Style identity across an action. A style is its name and kind, unique in the list before the edit. Its index, and the indexes that
# name it (P/@Style and STYLE/@NextStyle, the typed style references of HWPML2X), are numbers an action may renumber, so after the action
# each old style is found by its identity, and each reference is compared by the identity it resolves to in its own list.

def _identity(item: Mapping[str, Any]) -> tuple[str, str]:
    return item['name'], item['kind']


def _require_identities(model: Mapping[str, Any]) -> None:
    """Before anything is written: each style one name and kind, and each next style in the list, or the outcome cannot be proven by identity."""
    styles = model['styles']
    counts: dict[tuple[str, str], int] = {}
    for item in styles:
        counts[_identity(item)] = counts.get(_identity(item), 0) + 1
    for (name, kind), count in counts.items():
        if count > 1:
            raise NativeNumberingRefusal(f'{count} {kind} styles are named {name}, so which is which after the action cannot be proven by name; '
                                         'nothing was written', code='style_name_ambiguous')
    for item in styles:
        if not 0 <= item['next'] < len(styles):
            raise NativeNumberingRefusal(f"the style {item['name']} names a next style outside the style list, so its next style cannot be proven by "
                                         'name after the action; nothing was written', code='style_model_unusable')


def _settings(item: Mapping[str, Any]) -> dict[str, str]:
    """A style's settings but its NextStyle index, which is compared by the style it names."""
    return {key: value for key, value in item['settings'] if key != 'nextstyle'}


def _next_of(item: Mapping[str, Any], styles: list[dict[str, Any]]) -> dict[str, Any] | None:
    return styles[item['next']] if 0 <= item['next'] < len(styles) else None


def _named(name: str) -> str:
    return name[:NAME_MAX_CHARS]


def _fact(predicate: str, text: str, **fields: Any) -> dict[str, Any]:
    """One problem of the proof: its fixed predicate, the text the message shows, and the bounded facts the caller gets."""
    return {'predicate': predicate, 'text': text, **{key: value for key, value in fields.items() if value is not None}}


def _link(old: list[dict[str, Any]], new: list[dict[str, Any]], request_name: str | None = None) -> dict[str, Any]:
    """Each old style's index after the action, by its identity; for a create, the one new style of the requested name; and what is
    missing, doubled or not requested. Nothing is matched by appearance."""
    places: dict[tuple[str, str], list[int]] = {}
    for item in new:
        places.setdefault(_identity(item), []).append(item['index'])
    mapping: dict[int, int] = {}
    problems: list[dict[str, Any]] = []
    accounted: set[int] = set()
    for item in old:
        found = places.get(_identity(item), [])
        accounted.update(found)
        if len(found) == 1:
            mapping[item['index']] = found[0]
        elif not found:
            problems.append(_fact('old_style_missing', f"the old style {item['name']} is gone", name=_named(item['name']), old_index=item['index']))
        else:
            problems.append(_fact('old_style_duplicated', f"{len(found)} styles are named {item['name']} after the action", name=_named(item['name']),
                                  old_index=item['index']))
    others = [item for item in new if item['index'] not in accounted]
    added = None
    if request_name is not None:
        named = [item for item in others if item['name'] == request_name]
        if len(named) == 1:
            added = named[0]
        elif named:
            problems.append(_fact('new_style_duplicated', f'{len(named)} new styles are named {request_name}', name=_named(request_name)))
        else:
            problems.append(_fact('new_style_missing', f'no new style is named {request_name}', name=_named(request_name)))
        others = [item for item in others if item['name'] != request_name]
    problems += [_fact('style_added', f"a style {item['name']} that was not requested appeared", name=_named(item['name']), new_index=item['index'])
                 for item in others]
    return {'map': mapping, 'added': added, 'problems': problems}


def _stable(link: Mapping[str, Any], old: list[dict[str, Any]], new: list[dict[str, Any]]) -> bool:
    """Every old style at its old index with its old NextStyle index: no style reference was renumbered."""
    return len(link['map']) == len(old) and all(link['map'][item['index']] == item['index'] and new[item['index']]['next'] == item['next'] for item in old)


def _stand(side: Mapping[str, Any], style: Mapping[str, Any], what: str) -> tuple[int, int, int]:
    """The caret in the first body paragraph that uses this style and shows exactly its definitions: at its first text, or its end."""
    for item in side['styles']['paragraphs']:
        if item['style'] == style['index'] and item['context'] == 'body' and item['plain']:
            paragraph = side['model']['paragraphs'][item['census_index']]
            first = next((number for number, atom in enumerate(paragraph['atoms']) if 'char' in atom), len(paragraph['atoms']))
            return 0, paragraph['body_index'], nf._position_of(paragraph, first)  # noqa: SLF001
    raise NativeNumberingRefusal(f'no body paragraph uses {what} with exactly its definitions, so the native action has nowhere to read them from',
                                 code='style_without_plain_body_use')


def _enter(progress: dict[str, Any] | None) -> None:
    """Mark the first native navigation: from here a technical fault holds at once, and a clean refusal gives the caret back."""
    if progress is not None:
        progress['entered'] = True


def _entered_state(hwp: Any) -> tuple[tuple[int, int, int], tuple[int, ...]]:
    """The caret and selection after the edit entered native navigation. A read that raises, is not canonical or moves between
    its own reads is an unknown outcome at once; nothing further is called."""
    try:
        state = _read_target_state(hwp)
    except NativeNumberingReadFailure as exc:
        raise _held('caret.read', 'raised', 'the caret read after the edit entered native navigation failed') from exc
    except NativeNumberingRefusal as exc:
        returned = 'absent' if exc.code == 'native_unavailable' else 'not_canonical'
        raise _held('caret.read', returned, 'the caret read after the edit entered native navigation is not canonical') from exc
    except Exception as exc:
        raise _held('caret.read', 'raised', 'the caret read after the edit entered native navigation raised') from exc
    if state is None:
        raise _held('caret.read', 'not_canonical', 'the caret and selection read after the edit entered native navigation are not canonical, '
                    'or the caret moved between its reads')
    return state


# Native members.

_MISSING = object()
_PLAIN = (bool, int, float, complex, str, bytes, bytearray, tuple, list, dict, set, frozenset)
_DYNAMIC = ('__getattr__', '__getattribute__')


def _purely_absent(owner: Any, name: str) -> bool:
    """Reading the attribute ran no code but Python's own lookup, and found nothing: no attribute, descriptor or class member by
    that name (inspect.getattr_static, which triggers neither the descriptor protocol, __getattr__ nor __getattribute__), and no
    __getattr__ or __getattribute__ of the object's own classes, which getattr_static cannot see into."""
    if inspect.getattr_static(owner, name, _MISSING) is not _MISSING:
        return False
    for base in type.__dict__['__mro__'].__get__(type(owner)):
        if base is object:
            continue
        for key in type.__dict__['__dict__'].__get__(base):
            if type(key) is str and key in _DYNAMIC:
                return False
    return True


def _member(owner: Any, name: str, what: str, *, method: bool = False, site: str | None = None) -> Any:
    """One native member, read once. Raising, None, a plain value for an object or something that cannot be called for a method
    is an unknown outcome at once; only a purely absent attribute is the clean refusal native_unavailable. With a site, the
    unknown outcome carries that fixed label and a raise of any class is one too."""
    try:
        value = _sdk_read(lambda: getattr(owner, name))
    except AttributeError as exc:
        if _purely_absent(owner, name):
            raise NativeNumberingRefusal(f'native {what} is unavailable on this machine', code='native_unavailable') from exc
        raise _labelled(NativeNumberingReadFailure(f'native {what} could not be read: its lookup raised AttributeError, which is not absence; '
                                                   'nothing further is called', code=_UNPROVEN), site, 'raised') from exc
    except Exception as exc:
        if site is None:
            raise
        raise _held(site, 'raised', f'native {what} could not be read') from exc
    if method:
        if not callable(value):
            raise _labelled(NativeNumberingReadFailure(f'native {what} is a {type(value).__name__} value, not a method; nothing further is called',
                                                       code=_UNPROVEN), site, 'not_callable')
    elif value is None or issubclass(type(value), _PLAIN):
        raise _labelled(NativeNumberingReadFailure(f'native {what} is a {type(value).__name__} value, not an object; nothing further is called',
                                                   code=_UNPROVEN), site, _shape(value))
    return value


def _method(owner: Any, name: str, *, site: str | None = None) -> Callable[..., Any]:
    found = _member(owner, name, name, method=True, site=site)
    return lambda *args, **kwargs: _sdk_read(lambda: found(*args, **kwargs))


def _capabilities(hwp: Any, set_name: str) -> tuple[Any, Any]:
    """HAction with GetDefault and Execute, and HParameterSet.<set_name>: read before the edit moves anything."""
    haction = _member(hwp, 'HAction', 'HAction')
    for name in ('GetDefault', 'Execute'):
        _member(haction, name, f'HAction.{name}', method=True)
    sets = _member(hwp, 'HParameterSet', 'HParameterSet')
    return haction, _member(sets, set_name, f'HParameterSet.{set_name}')


def _place(hwp: Any, pos: tuple[int, int, int]) -> None:
    """native_numbering._set_pos once the edit has entered native navigation, with set_pos read by _member: SetPos is read only
    where set_pos is purely absent, and a caret method that cannot be read is an unknown outcome at once."""
    for name in ('set_pos', 'SetPos'):
        try:
            mover = _method(hwp, name, site='caret.method')
        except NativeNumberingReadFailure:
            raise
        except NativeNumberingRefusal:
            continue
        try:
            result = mover(pos[0], pos[1], pos[2])
        except Exception as exc:
            raise _held('caret.place', 'raised', 'the native caret placement raised') from exc
        if result is not None and result is not True:
            raise _held('caret.place', _shape(result), 'the native caret placement was not confirmed')
        return
    raise _held('caret.method', 'absent', 'native set_pos and SetPos are both absent after the edit entered native navigation')


def _caret_at(hwp: Any, caret: tuple[int, int, int], progress: dict[str, Any] | None) -> None:
    _enter(progress)
    _place(hwp, caret)
    if _entered_state(hwp) != (caret, ()):
        raise NativeNumberingRefusal('the caret is somewhere else than the paragraph that shows the style', code='selection_unverified')


def _native_list(hwp: Any, rows: list[dict[str, Any]], progress: dict[str, Any] | None) -> tuple[int, int]:
    """native_format._native_list under this module's boundary: (list, first paragraph); a table cell's list from a native search."""
    head = rows[0]['paragraph']
    if head['context'] == 'body':
        return 0, head['body_index']
    wanted = head['text'] if len(head['text']) <= STEP_MATCH_MAX_CHARS else head['text'][:STEP_MATCH_MAX_CHARS]
    if not wanted.strip():
        raise NativeNumberingRefusal('a table cell paragraph without text cannot be located in the native document', code='target_unlocatable')
    mover = _method(hwp, 'MoveDocBegin')
    finder = _method(hwp, 'find')
    _enter(progress)
    _require_motion_result(mover())
    found: list[tuple[int, int, int]] = []
    for _ in range(nf._FIND_HITS_MAX):  # noqa: SLF001
        matched = finder(wanted, direction='Forward', MatchCase=1, WholeWordOnly=0)
        if type(matched) is not bool:
            raise NativeNumberingReadFailure(f'native find returned a {type(matched).__name__} value, not a BOOL; nothing further is called', code=_UNPROVEN)
        if matched is False:
            break
        selection = _get_selection(hwp)
        if selection is None:
            raise NativeNumberingReadFailure('the selection read after a text match is not canonical; nothing further is called', code=_UNPROVEN)
        if not selection or selection[0] != selection[3] or selection[1] != selection[4]:
            raise NativeNumberingRefusal('a text match could not be located by position', code='target_unlocatable')
        found.append((selection[0], selection[1], selection[2]))
        _place(hwp, (selection[3], selection[4], selection[5]))
    else:
        raise NativeNumberingRefusal('text search did not terminate', code='target_unlocatable')
    cells = [hit for hit in found if hit[0] != 0 and hit[2] == nf._position_of(head, nf._char_indexes(head)[0])]  # noqa: SLF001
    if not cells:
        raise NativeNumberingRefusal('the table cell text could not be found in the native document', code='target_unlocatable')
    if len(cells) > 1:
        raise NativeNumberingRefusal('more than one table cell holds this whole text, so which one is meant cannot be proven', code='target_ambiguous')
    return cells[0][0], cells[0][1]


def _select(hwp: Any, block: tuple[int, ...], words: list[str] | None, page_of: Callable[[], int], expected_page: int, progress: dict[str, Any] | None) -> None:
    """native_format._select under this module's boundary: a well-typed other answer refuses, a malformed one holds."""
    list_id, start_para, start_pos, _end_list, end_para, end_pos = block
    _enter(progress)
    _place(hwp, (list_id, start_para, start_pos))
    nf._require_page(page_of(), expected_page)  # noqa: SLF001
    selected = _sdk_read(lambda: hwp.select_text(start_para, start_pos, end_para, end_pos, list_id))
    if selected is False:
        raise NativeNumberingRefusal('the requested range could not be selected', code='selection_unverified')
    if selected is not True:
        raise NativeNumberingReadFailure(f'native select_text returned a {type(selected).__name__} value, not a BOOL; nothing further is called', code=_UNPROVEN)
    selection = _get_selection(hwp)
    if selection is None:
        raise NativeNumberingReadFailure('the selection read after select_text is not canonical; nothing further is called', code=_UNPROVEN)
    if selection != block:
        raise NativeNumberingRefusal('the selected block is not the requested range', code='selection_unverified')
    if words is not None:
        held = nf._block_words(hwp)  # noqa: SLF001
        if held is None:
            raise NativeNumberingReadFailure("the selected block's own text could not be read as a document; nothing further is called", code=_UNPROVEN)
        if held != words:
            raise NativeNumberingRefusal('the selected block does not hold exactly the requested text', code='selection_unverified')
    if _entered_state(hwp)[1] != block:
        raise NativeNumberingRefusal('the selection was not held after it was checked', code='selection_unverified')


def _defaults(haction: Any, action: str, pset: Any) -> None:
    """GetDefault is documented BOOL: True goes on, False is a clean refusal, any other value an unknown outcome at once.

    The set's HSet, which GetDefault takes, is read first as an object (_member). The edit has entered native navigation by now,
    so an HSet read that raises, returns None or a plain value, or finds no HSet at all is an unknown outcome at once, and
    GetDefault is not called."""
    try:
        hset = _member(pset, 'HSet', f'{action} parameter set HSet')
    except NativeNumberingReadFailure:
        raise
    except NativeNumberingRefusal as exc:
        raise NativeNumberingReadFailure(f'the native {action} parameter set has no HSet after the edit entered native navigation; nothing further is called',
                                         code=_UNPROVEN) from exc
    result = _sdk_read(lambda: haction.GetDefault(action, hset))
    if result is True:
        return
    if result is False:
        raise NativeNumberingRefusal(f'native {action} defaults could not be read into this parameter set (GetDefault returned False); nothing was written',
                                     code='style_action_unavailable')
    raise NativeNumberingReadFailure(f'native {action} GetDefault returned a {type(result).__name__} value, not the documented BOOL; nothing further is called',
                                     code=_UNPROVEN)


def _write(target: Any, items: Mapping[str, Any]) -> None:
    """A parameter write that raises, after the edit entered native navigation, is an unknown outcome at once."""
    for name, value in items.items():
        try:
            setattr(target, name, value)
        except Exception as exc:
            raise NativeNumberingReadFailure(f'native parameter {name} could not be set: exception of class {_exception_class_label(exc)}; '
                                             'nothing further is called', code=_UNPROVEN) from exc


def _execute(haction: Any, action: str, pset: Any, reach: _MutationReach) -> None:
    reach.issue()
    result = haction.Execute(action, pset.HSet)
    if not _action_confirmed(result):
        raise NativeNumberingMutationError(f'native {action} returned {_status_text(result)} instead of the documented BOOL True; nothing was rolled back')


def _leave(hwp: Any, restore: tuple[int, int, int]) -> None:
    _place(hwp, restore)
    if _get_selection(hwp) != ():
        raise NativeNumberingMutationError('the selection did not end after the style edit; nothing was rolled back')


# The legacy action family (HwpAutomation_2504 pages 61-64) of the create and change commands.

def _is_none(value: Any) -> bool:
    return value is None


def _is_bool(value: Any) -> bool:
    return value is True or value is False


def _is_object(value: Any) -> bool:
    return value is not None and not issubclass(type(value), _PLAIN)


def _get_default_answer(action: str) -> Callable[[Any], bool]:
    """The exact builtin int GetDefault answered natively for the action: the type first, so no bool, int subclass or foreign
    object is ever compared."""
    observed = _GET_DEFAULT_OBSERVED[action]
    return lambda value: type(value) is int and value in observed


def _legacy_call(site: str, call: Callable[[], Any], accept: Callable[[Any], bool], what: str) -> Any:
    """One call of the legacy family before the action is issued: a raise, or an answer other than the one documented for it, is an
    unknown outcome at once with the call's fixed site."""
    try:
        value = _sdk_read(call)
    except Exception as exc:
        raise _held(site, 'raised', f'native {what} raised') from exc
    if not accept(value):
        raise _held(site, _shape(value), f'native {what} did not answer as documented')
    return value


def _entered_member(owner: Any, name: str, what: str, site: str) -> Any:
    """A method read once the edit has entered native navigation: even a purely absent one is an unknown outcome, never a refusal."""
    try:
        return _member(owner, name, what, method=True, site=site)
    except NativeNumberingReadFailure:
        raise
    except NativeNumberingRefusal as exc:
        raise _held(site, 'absent', f'native {what} is absent after the edit entered native navigation') from exc


def _legacy(hwp: Any, action: str, set_methods: tuple[str, ...], *, entered: bool = False) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """CreateAction(action), its CreateSet, GetDefault and Execute, the set CreateSet returns and its set_methods, each read once and
    kept. A create reads them before the edit moves anything, where a purely absent member is the clean native_unavailable. A change
    reads them after the caret moved and the style's shape was read (``entered``): there a purely absent member holds at its site, as
    every entered member does (_entered_member)."""
    if entered:
        read = _entered_member
    else:
        def read(owner: Any, name: str, what: str, site: str) -> Any:
            return _member(owner, name, what, method=True, site=site)
    creator = read(hwp, 'CreateAction', 'CreateAction', 'create_action.member')
    made = _legacy_call('create_action.call', lambda: creator(action), _is_object, f'CreateAction({action})')
    methods = {name: read(made, name, f'{action} {name}', 'action.member') for name in ('CreateSet', 'GetDefault', 'Execute')}
    pset = _legacy_call('create_set.call', lambda: methods['CreateSet'](), _is_object, f'{action} CreateSet')
    calls = {name: read(pset, name, f'{action} set {name}', 'set.member') for name in set_methods}
    return methods, pset, calls


def _set_items(setter: Callable[..., Any], items: Mapping[str, Any], action: str, prefix: str) -> None:
    for key, value in items.items():
        _legacy_call(f'{prefix}.{key}', lambda: setter(key, value), _is_none, f'{action} SetItem {key}')


def _filled_char_shape(calls: Mapping[str, Callable[..., Any]], action: str, character: Mapping[str, Any], refusal: str) -> None:
    """The CharShape sub-set GetDefault filled, with only the requested items written into it, set back on its parent once.

    A sub-set made with CreateItemSet needs every one of its child items set, and a partial change edits the one GetDefault filled
    (Hancom, topic 1044 post 6), so none is made here. Under Automation the filled one Item gives is a copy (topic 2354 post 5),
    so it is set back with SetItem. ItemExist's well-typed False is the clean refusal ``refusal``, before anything is written;
    every other answer is checked as declared and holds at its site."""
    if not _legacy_call('item_exist.CharShape', lambda: calls['ItemExist']('CharShape'), _is_bool, f'{action} ItemExist CharShape'):
        raise NativeNumberingRefusal(refusal, code='style_variant_unsupported')
    chars = _legacy_call('item.CharShape', lambda: calls['Item']('CharShape'), _is_object, f'{action} Item CharShape')
    _set_items(_entered_member(chars, 'SetItem', f'{action} CharShape SetItem', 'subset.member'), nf._character_items(character),  # noqa: SLF001
               action, 'subset_set_item')
    _set_items(calls['SetItem'], {'CharShape': chars}, action, 'set_item')


# The whole character shape a change writes: IHwpObject.CharShape at the style's plain use (HwpAutomation_2504 page 12: the selection's
# character shape, or the caret's without one, as a CharShape parameter set; an item whose value differs within the selection is
# absent). Its items are ParameterSetTable_2504 set 13, the same 67 as HCharShape of the installed type library, which spells
# OutlineType as OutLineType: an item is there when either spelling is.
_CHAR_SHAPE_ITEMS = (*((f'{kind}{language}',) for kind in ('FaceName', 'FontType', 'Size', 'Ratio', 'Spacing', 'Offset') for language in nf.LANGUAGES),
                     *((name,) for name in ('Bold', 'Italic', 'SmallCaps', 'Emboss', 'Engrave', 'SuperScript', 'SubScript', 'UnderlineType')),
                     ('OutLineType', 'OutlineType'),
                     *((name,) for name in ('ShadowType', 'TextColor', 'ShadeColor', 'UnderlineShape', 'UnderlineColor', 'ShadowOffsetX', 'ShadowOffsetY',
                                            'ShadowColor', 'StrikeOutType', 'DiacSymMark', 'UseFontSpace', 'UseKerning', 'Height', 'StrikeOutShape',
                                            'StrikeOutColor', 'BorderFill')))
_UNDERLINE_CODES = {'none': 0, 'bottom': 1, 'center': 2, 'top': 3}  # ParameterSetTable_2504 set 13, UnderlineType
_COLOR_ITEMS = frozenset({'TextColor'})
_NUMBER = re.compile(r'-?[0-9]{1,10}')


def _number(text: Any) -> int | None:
    value = (text or '').strip() if type(text) is str else ''
    return int(value) if _NUMBER.fullmatch(value) else None


def _bound_items(props: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """The CharShape items whose value the document text tells for this character shape (native_format._char_props), in the order
    they are read: size, colour, bold, italic, the underline's type, and per language the font name, ratio, spacing, relative size and
    offset. An item the text does not tell, or tells in a way that has no documented code, is not among them."""
    attrs, children = props['attrs'], props['children']
    out: list[tuple[str, Any]] = []
    for item, key in (('Height', 'height'), ('TextColor', 'textcolor')):
        value = _number(attrs.get(key))
        if value is not None:
            out.append((item, value))
    out += [('Bold', 1 if props['bold'] else 0), ('Italic', 1 if props['italic'] else 0)]
    underline = _UNDERLINE_CODES.get(nf._underline_type(props))  # noqa: SLF001
    if underline is not None:
        out.append(('UnderlineType', underline))
    for language in nf.LANGUAGES:
        key = language.lower()
        name = props['fonts'].get(key)
        if type(name) is str:
            out.append((f'FaceName{language}', name))
        for item, child in (('Ratio', 'ratio'), ('Spacing', 'charspacing'), ('Size', 'relsize'), ('Offset', 'charoffset')):
            value = _number((children.get(child) or {}).get(key))
            if value is not None:
                out.append((f'{item}{language}', value))
    return out


# The PIT type of each item _bound_items reads (ParameterSetTable_2504 set 13, pages 16-18), and the values each type represents:
# UI1 an unsigned and I1 a signed 8-bit value, I4 a signed 32-bit value, and UI4 an unsigned 32-bit COLORREF, which a VARIANT may
# carry signed. Only these representations: no narrower usable range of a size, ratio, spacing or offset is imposed.
_TOLD_PIT = {'Height': 'I4', 'TextColor': 'UI4', 'Bold': 'UI1', 'Italic': 'UI1', 'UnderlineType': 'UI1',
             **{f'{kind}{language}': pit for kind, pit in (('FaceName', 'BSTR'), ('Ratio', 'UI1'), ('Size', 'UI1'), ('Spacing', 'I1'), ('Offset', 'I1'))
                for language in nf.LANGUAGES}}
_PIT_RANGES = {'UI1': (0, 0xFF), 'I1': (-0x80, 0x7F), 'I4': (-(1 << 31), (1 << 31) - 1), 'UI4': (-(1 << 31), (1 << 32) - 1)}


def _pit_type(item: str) -> str:
    return _TOLD_PIT[item]


def _item_answer(item: str) -> Callable[[Any], bool]:
    """A font name is a str (PIT_BSTR); every other item read is an exact int (never a bool or an int subclass) within the values its
    PIT type represents. An answer outside them is malformed, not a value that differs from the style's: it holds at char_shape.item."""
    pit = _pit_type(item)
    if pit == 'BSTR':
        return lambda value: type(value) is str
    low, high = _PIT_RANGES[pit]
    return lambda value: type(value) is int and low <= value <= high


def _same_item(item: str, value: Any, expected: Any) -> bool:
    if item in _COLOR_ITEMS:
        # A COLORREF is 32 bits, read as an unsigned or a signed VARIANT.
        return -(1 << 31) <= value < (1 << 32) and value & 0xFFFFFFFF == expected & 0xFFFFFFFF
    return value == expected


def _style_char_shape(hwp: Any, style: Mapping[str, Any]) -> tuple[Any, Callable[..., Any]]:
    """The style's whole character shape as a CharShape parameter set, and its SetItem: IHwpObject.CharShape read with the caret in the
    style's plain body use and nothing selected (_caret_at), before the change's action is made, as no action may be made while another
    awaits its Execute. Every documented item must be there (ItemExist, exactly a BOOL), and every item the document text tells must
    hold the style's own value (Item, exactly its type and within the values its PIT type represents): a well-formed answer otherwise
    is the clean refusal style_shape_unverified, before anything is written. A read, member or answer of any other kind, an Item
    outside its PIT type's values included, holds at once."""
    try:
        shape = _member(hwp, 'CharShape', 'CharShape', site='char_shape.read')
    except NativeNumberingReadFailure:
        raise
    except NativeNumberingRefusal as exc:
        raise _held('char_shape.read', 'absent', 'native CharShape is absent after the edit entered native navigation') from exc
    exist, item, write = (_entered_member(shape, name, f'CharShape {name}', 'char_shape.member') for name in ('ItemExist', 'Item', 'SetItem'))
    for names in _CHAR_SHAPE_ITEMS:
        if not any(_legacy_call('char_shape.item_exist', lambda: exist(name), _is_bool, f'CharShape ItemExist {name}') for name in names):
            raise NativeNumberingRefusal(f"the character shape read at the style's use holds no item {' or '.join(names)}, so it is not the whole shape "
                                         'the change must write; nothing was written', code='style_shape_unverified')
    for name, expected in _bound_items(style['char']):
        value = _legacy_call('char_shape.item', lambda: item(name), _item_answer(name), f'CharShape Item {name}')
        if not _same_item(name, value, expected):
            raise NativeNumberingRefusal(f"the character shape read at the style's use is not the style's own: its item {name} differs from the "
                                         'document text; nothing was written', code='style_shape_unverified')
    return shape, write


def _legacy_execute(execute: Callable[..., Any], pset: Any, action: str, reach: _MutationReach) -> None:
    """Execute is declared VT_BOOL (dispid 15002 of the installed IDHwpAction), and the same IDHwpAction.Execute answered True
    natively for this repository's InsertText, CharShape and ParagraphShape commands. Exactly True goes on and the document
    proves the rest; any other answer comes after the action, an unknown outcome named by its fixed shape only."""
    reach.issue()
    result = execute(pset)
    if result is not True:
        raise NativeNumberingMutationError(f'native {action} Execute answered {_shape(result)}, not the True of its declared BOOL; '
                                           'nothing was rolled back')


# The proof.

def _content(item: Mapping[str, Any]) -> list[Any]:
    """Every character and control with its data, and the settings of the run and CHAR holding it."""
    return [[atom.get(key) for key in ('char', 'control', 'digest', 'data', 'run', 'holder')] for atom in item['atoms']]


def _shapes(item: Mapping[str, Any]) -> list[Any]:
    """Each character's and control's character shape by its whole content, its fonts' whole content included."""
    return [atom['shape_sha'] for atom in item['atoms']]


def _where(item: Mapping[str, Any]) -> str:
    return f"paragraph {item['census_index']}" if item['area'] is None else f"{item['area']} paragraph {item['area_paragraph']}"


def _kept(one: Mapping[str, Any], two: Mapping[str, Any], *, inst: bool) -> bool:
    """Everything of a paragraph but its style index, its shapes and (when ``inst`` is false) its InstId."""
    return (_content(one) == _content(two) and one['settings'] == two['settings'] and one['extra'] == two['extra'] and one['end_run'] == two['end_run']
            and (not inst or one['inst'] == two['inst']))


def _same_content(one: Mapping[str, Any], two: Mapping[str, Any]) -> bool:
    """Everything of a paragraph but the style it names, which is compared by identity."""
    return _kept(one, two, inst=True) and one['para_sha'] == two['para_sha'] and _shapes(one) == _shapes(two) and one['end_sha'] == two['end_sha']


def _paragraph_reference(one: Mapping[str, Any], two: Mapping[str, Any], link: Mapping[str, Any], old: list[dict[str, Any]],
                         new: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A paragraph's style after the action is the very style it named before, by identity, wherever that style now is. A paragraph
    whose old style is gone or doubled is already a problem of the list (_link)."""
    mapped = link['map'].get(one['style'])
    if mapped is None or two['style'] == mapped:
        return None
    was, now = old[one['style']]['name'], new[two['style']]['name']
    return _fact('paragraph_style', f'{_where(one)} names the style {now}, not {was}', where=_fact_where(one), expected=_named(was), found=_named(now))


_AREA_ID = re.compile(r'(?:header|footer):s[0-9]{1,4}:[a-z]{0,8}')
_TOKEN = re.compile(r'[a-z0-9_]{1,40}')


def _fact_where(item: Mapping[str, Any]) -> str:
    """Where a paragraph is, for the facts: its census index, or its header or footer and its place there. Never its text."""
    if item['area'] is None:
        return f"paragraph {item['census_index']}"
    area = item['area'] if _AREA_ID.fullmatch(item['area']) else item['context']
    return f"{area} paragraph {item['area_paragraph']}"


def _problems(before: Mapping[str, Any], after: Mapping[str, Any], link: Mapping[str, Any], *,
              targets: Mapping[int, Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], list[str]]],
              styles: Callable[[list[dict[str, Any]], list[dict[str, Any]], Mapping[str, Any]], list[dict[str, Any]]],
              lists: frozenset[str], removals_only: bool) -> list[dict[str, Any]]:
    """The whole document against its expected state: ``targets`` checks the paragraphs (by row number) allowed to change and
    ``styles`` the style list, through ``link`` (_link); every other paragraph and everything else must be exactly as before, each
    style reference naming the same style by identity."""
    census_before, census_after = before['census'], after['census']
    old, new = before['styles'], after['styles']
    in_areas = any(old['paragraphs'][number]['area'] is not None for number in targets)
    problems = [_fact('document_changed', f'{field} changed', field=field) for field in (*_COMPARED, 'frame')
                if field not in {'styles_sha256', 'header_footer'} and census_before.get(field) != census_after.get(field)]
    # The census digest of a header or footer names each paragraph's style by the style element's own text, its NextStyle index
    # included, so it holds only while no style was renumbered. The same header or footer without its paragraphs' style references
    # is always compared, and those references by identity with the paragraphs below.
    if not in_areas and ((_stable(link, old['styles'], new['styles']) and census_before.get('header_footer') != census_after.get('header_footer'))
                         or [item['content'] for item in old['areas']] != [item['content'] for item in new['areas']]):
        problems.append(_fact('document_changed', 'header_footer changed', field='header_footer'))
    for name in sorted(set(census_before['definitions']) | set(census_after['definitions'])):
        if name == 'stylelist':
            continue
        removed, added = _multiset_delta(census_before['definitions'].get(name, []), census_after['definitions'].get(name, []))
        if (removed or added) and (name not in lists or (removals_only and added)):
            problems.append(_fact('document_changed', f'{name} changed', field=name if _TOKEN.fullmatch(name) else 'definitions'))
    problems += link['problems']
    problems += styles(old['styles'], new['styles'], link)
    if [[item[key] for key in ('id', 'order', 'shell')] for item in old['areas']] != [[item[key] for key in ('id', 'order', 'shell')] for item in new['areas']]:
        problems.append(_fact('document_changed', 'a header or footer frame changed', field='header_footer_frame'))
    if len(old['paragraphs']) != len(new['paragraphs']):
        problems.append(_fact('document_changed', 'the paragraph count changed', field='paragraph_count'))
        return problems
    for number, (one, two) in enumerate(zip(old['paragraphs'], new['paragraphs'])):
        where = _fact_where(one)
        if [one[key] for key in ('context', 'census_index', 'area', 'area_paragraph')] != [two[key] for key in ('context', 'census_index', 'area', 'area_paragraph')]:
            problems.append(_fact('paragraph_moved', f'{_where(one)} moved', where=where))
            continue
        check = targets.get(number)
        if check is not None:
            problems += [_fact('target_paragraph', text, where=where) for text in check(one, two, link)]
            continue
        reference = _paragraph_reference(one, two, link, old['styles'], new['styles'])
        if reference is not None:
            problems.append(reference)
        if not _same_content(one, two):
            problems.append(_fact('paragraph_changed', f'{_where(one)} outside the request changed', where=where))
    return problems


CHARACTER_ITEMS_SHOWN = style_rules.CHARACTER_ITEMS_SHOWN
_CHARACTER_PARTS = frozenset({'attrs', 'fonts', 'font_types', 'children', 'bold', 'italic', 'underline'})


def _size_points(props: Mapping[str, Any]) -> float | None:
    text = (props['attrs'].get('height') or '').strip()
    if not text.isdigit() or len(text) > 9:
        return None
    size = int(text) / 100
    return size if size <= style_rules.SIZE_MAX_PT else None


def _requested_item(item: str, wanted: Mapping[str, Any]) -> bool:
    if item.startswith(('font_', 'font_type_', 'font_content_')):
        return 'font_family' in wanted
    return {'attr_height': 'size_pt', 'attr_textcolor': 'text_color', 'bold': 'bold', 'italic': 'italic', 'underline': 'underline'}.get(item) in wanted


def _character_facts(actual: Mapping[str, Any], expected: Mapping[str, Any], wanted: Mapping[str, Any], actual_fonts: Mapping[str, str] | None,
                     expected_fonts: Mapping[str, str] | None) -> dict[str, Any]:
    """What of a character shape (native_format._char_props) differs from the one expected, read from the models the proof already has:
    the size expected and found in points, the differing items by fixed label and how many there are, and whether any of them was
    not requested. An item is a CHARSHAPE attribute (attr_<name>), bold, italic or underline, the font, font type or whole font
    content of a language (font_<language>, font_type_<language>, font_content_<language>, when both contents are known), or another
    child element (child_<name>). Labels and counts only, never a value; a label that is no fixed token is counted, not shown."""
    items: list[str] = []

    def differ(prefix: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> None:
        items.extend(f'{prefix}{key}' for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key))

    differ('attr_', actual['attrs'], expected['attrs'])
    items.extend(name for name in ('bold', 'italic') if actual[name] != expected[name])
    if not nf._same_character({**expected, 'underline': actual['underline']}, expected, wanted):  # noqa: SLF001
        items.append('underline')
    differ('font_', actual['fonts'], expected['fonts'])
    differ('font_type_', actual['font_types'], expected['font_types'])
    differ('child_', actual['children'], expected['children'])
    if actual_fonts is not None and expected_fonts is not None:
        differ('font_content_', actual_fonts, expected_fonts)
    if any(actual.get(key) != expected.get(key) for key in (set(actual) | set(expected)) - _CHARACTER_PARTS):
        items.append('other')
    facts: dict[str, Any] = {}
    for key, props in (('size_expected', expected), ('size_found', actual)):
        size = _size_points(props)
        if size is not None:
            facts[key] = size
    shown = [item for item in items if _TOKEN.fullmatch(item)][:CHARACTER_ITEMS_SHOWN]
    if shown:
        facts['character_items'] = shown
    facts['character_item_count'] = len(items)
    facts['unrequested_changed'] = any(not _requested_item(item, wanted) for item in items)
    return facts


def _shape_facts(*args: Any) -> dict[str, Any]:
    """The character facts, or none when measuring them fails: the problem, its predicate, fields and the hold stay as they are."""
    try:
        return _character_facts(*args)
    except Exception:  # noqa: BLE001
        return {}


_KEPT_FIELD_TEXT = {'character': 'its character shape', 'paragraph': 'its paragraph shape'}


def _kept_style(item: Mapping[str, Any], later: Mapping[str, Any], old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One old style against itself after the action: every setting, both definitions by their whole content (fonts and TYPEINFO
    included), and its next style by the identity it names, one step and no further."""
    mine, theirs = _settings(item), _settings(later)
    attributes = sorted(key for key in set(mine) | set(theirs) if mine.get(key) != theirs.get(key))
    fields = ['settings'] if attributes else []
    texts = [f"its settings ({', '.join(attributes)})"] if attributes else []
    for field, key in (('character', 'char_sha256'), ('paragraph', 'para_sha256')):
        if item[key] != later[key]:
            fields.append(field)
            texts.append(_KEPT_FIELD_TEXT[field])
    problems = []
    following, now = _next_of(item, old), _next_of(later, new)
    expected = found = None
    if now is None:
        problems.append(_fact('next_style_dangling', f"the style {later['name']} names a next style outside the style list", name=_named(later['name']),
                              new_index=later['index']))
    elif following is not None and _identity(following) != _identity(now):
        fields.append('next')
        expected, found = following['name'], now['name']
        texts.append(f'its next style ({now["name"]}, not {following["name"]})')
    if fields:
        shape = _shape_facts(later['char'], item['char'], {}, later['char_fonts'], item['char_fonts']) if 'character' in fields else {}
        problems.insert(0, _fact('old_style_changed', f"the old style {item['name']} changed {', '.join(texts)}", name=_named(item['name']),
                                 old_index=item['index'], new_index=later['index'], fields=fields,
                                 attributes=[key for key in attributes if _TOKEN.fullmatch(key)][:16] or None, expected=_named(expected) if expected else None,
                                 found=_named(found) if found else None, **shape))
    return problems


def _unchanged_styles(old: list[dict[str, Any]], new: list[dict[str, Any]], link: Mapping[str, Any], *, skip: int | None = None) -> list[dict[str, Any]]:
    """Every old style the edit does not change, against itself after the action wherever it now is."""
    problems = []
    for item in old:
        found = link['map'].get(item['index'])
        if found is not None and item['index'] != skip:
            problems += _kept_style(item, new[found], old, new)
    return problems


def _report_paragraph(item: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, Any]:
    segments = [{key: value for key, value in row.items() if key not in {'rest', 'underline_style'}} for row in nf._segments(item['atoms'])]  # noqa: SLF001
    return {'style': model['styles'][item['style']]['name'], 'paragraph': nf.public_paragraph(item['para']), 'segments': segments[:nf.REPORT_ITEMS],
            'segments_cut': len(segments) > nf.REPORT_ITEMS}


def _side_digest(side: Mapping[str, Any]) -> str:
    return _digest([side['census'].get(field) for field in (*_COMPARED, 'frame', 'body_sha256')] + [side['styles']['styles_hash']]
                   + [[item['style'], _content(item), item['text']] for item in side['styles']['paragraphs']])


def _undo(changed: bool, before: Mapping[str, Any], after: Mapping[str, Any] | None) -> dict[str, Any]:
    return {'native_editing_actions': 1 if changed else 0, 'pre_state_sha256': _side_digest(before) if changed else None,
            'post_state_sha256': _side_digest(after) if changed and after is not None else None}


def _guarded(reach: _MutationReach, what: str, body: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return body()
    except NativeNumberingMutationError:
        raise
    except Exception as exc:
        if not reach.reached:
            raise
        raise NativeNumberingMutationError(
            f'{what} failed with an exception of class {_exception_class_label(exc)} after the native call was issued; '
            'the document state is not proven and nothing was rolled back') from exc


_FACT_KEYS = ('predicate', 'name', 'old_index', 'new_index', 'fields', 'attributes', 'expected', 'found', 'where', 'field', *style_rules.CHARACTER_FACT_KEYS)
_MESSAGE_BUDGET = 560


def _not_as_requested(what: str, command: str, problems: list[dict[str, Any]], link: Mapping[str, Any], old: list[dict[str, Any]],
                      new: list[dict[str, Any]]) -> NativeNumberingMutationError:
    """The held outcome, measured before it is raised: a message of the first problems by identity within a fixed budget, and every
    problem counted in bounded facts (style_rules.style_proof_facts) that name styles, fields and places, never document text."""
    shown: list[str] = []
    used = 0
    for item in problems:
        text = item['text'][:200]
        if shown and used + len(text) + 2 > _MESSAGE_BUDGET:
            break
        shown.append(text)
        used += len(text) + 2
    rest = len(problems) - len(shown)
    error = NativeNumberingMutationError(f'{what} outcome is not as requested: ' + '; '.join(shown) + (f'; and {rest} more' if rest else '')
                                         + '; nothing was rolled back')
    added = link['added']
    error.style_proof = {
        'schema': style_rules.STYLE_PROOF_SCHEMA, 'command': command, 'old_styles': len(old), 'new_styles': len(new), 'matched': len(link['map']),
        'added': {'name': _named(added['name']), 'index': added['index']} if added is not None else None,
        'problem_count': len(problems),
        'problems': [{key: item[key] for key in _FACT_KEYS if key in item} for item in problems[:style_rules.STYLE_PROOF_SHOWN]],
        'problems_cut': len(problems) > style_rules.STYLE_PROOF_SHOWN,
    }
    return error


# Applying a style.

def _paragraph_check(style: Mapping[str, Any], old_styles: list[dict[str, Any]], outcomes: dict[int, str]
                     ) -> Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], list[str]]:
    def check(one: Mapping[str, Any], two: Mapping[str, Any], link: Mapping[str, Any]) -> list[str]:
        where = _where(one)
        problems = []
        if not _kept(one, two, inst=False):
            problems.append(f'{where} changed its text, controls or settings')
        if two['style'] != link['map'].get(style['index']):
            problems.append(f'{where} does not name the style')
        if two['para_sha'] != style['para_sha256']:
            problems.append(f"{where} does not have the style's paragraph shape")
        previous, target = old_styles[one['style']]['char_sha256'], style['char_sha256']
        reset = ([target] * len(one['atoms']), target if one['end_sha'] is not None else None)
        kept = ([target if shape == previous else shape for shape in _shapes(one)],
                (target if one['end_sha'] == previous else one['end_sha']) if one['end_sha'] is not None else None)
        actual = (_shapes(two), two['end_sha'])
        if actual == reset:
            outcomes[one['number']] = 'style'
        elif actual == kept:
            outcomes[one['number']] = 'kept_direct'
        else:
            problems.append(f"{where} has characters other than the style's or their own")
        return problems

    return check


def _character_check(style: Mapping[str, Any], span: tuple[int, int, int, int]) -> Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], list[str]]:
    low, high, lead, trail = span

    def check(one: Mapping[str, Any], two: Mapping[str, Any], link: Mapping[str, Any]) -> list[str]:
        where = _where(one)
        problems = []
        if not _kept(one, two, inst=False) or link['map'].get(one['style']) != two['style'] or one['para_sha'] != two['para_sha']:
            problems.append(f'{where} changed more than its characters')
        target = style['char_sha256']
        expected = [target if low <= number < high + trail or number < lead else shape for number, shape in enumerate(_shapes(one))]
        if _shapes(two) != expected:
            problems.append(f"{where} has characters other than requested")
        reaches = high + trail >= len(one['atoms'])
        if two['end_sha'] != one['end_sha'] and not (reaches and two['end_sha'] == target):
            problems.append(f'{where} changed its last run')
        return problems

    return check


def apply_style(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], page_of: Callable[[], int],
                restore: tuple[int, int, int], on_mutating: Callable[[], None] | None = None, progress: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind the style and the target again from the document text, select the target, issue one Style and prove the document."""
    reach = _MutationReach(on_mutating)

    def body() -> dict[str, Any]:
        before = read()
        _require_identities(before['styles'])
        style = _bound(before['styles'], request['style_name'], request['expected_style_hash'])
        rows, target_kind = nf._resolve(before['model'], request)  # noqa: SLF001
        numbers = {item['census_index']: item['number'] for item in before['styles']['paragraphs'] if item['census_index'] is not None}
        paragraph_style = style['kind'] == 'paragraph'
        targets: dict[int, Callable[..., list[str]]] = {}
        outcomes: dict[int, str] = {}
        if paragraph_style:
            already = all(before['styles']['paragraphs'][numbers[row['paragraph']['index']]]['style'] == style['index']
                          and before['styles']['paragraphs'][numbers[row['paragraph']['index']]]['plain'] for row in rows)
            check = _paragraph_check(style, before['styles']['styles'], outcomes)
            targets = {numbers[row['paragraph']['index']]: check for row in rows}
        else:
            if rows[0]['first'] is None or rows[-1]['last'] is None:
                raise NativeNumberingRefusal('the target has no text to take a character style', code='target_empty')
            nf._require_no_control_inside(rows)  # noqa: SLF001
            for position, row in enumerate(rows):
                if nf._inherits_mark(row['paragraph']) and nf._reaches_end(row, last=position == len(rows) - 1):  # noqa: SLF001
                    raise NativeNumberingRefusal("the range reaches the end of a numbered paragraph, whose mark takes its last run's shape; end the range earlier",
                                                 code='target_changes_number_mark')
            shapes = {number: _shapes(before['styles']['paragraphs'][numbers[row['paragraph']['index']]]) for number, row in enumerate(rows)}
            already = all(shape == style['char_sha256'] for number, row in enumerate(rows) for shape in shapes[number][row['first'] or 0:row['last'] or 0])
            if not already:
                nf._require_measured_neighbours(rows)  # noqa: SLF001
            for position, row in enumerate(rows):
                atoms = row['paragraph']['atoms']
                low = 0 if position else row['first'] or 0
                high = len(atoms) if position < len(rows) - 1 else row['last'] or 0
                lead, trail = nf._joined(atoms, low, high)  # noqa: SLF001
                targets[numbers[row['paragraph']['index']]] = _character_check(style, (low, high, lead, trail))
        if not already:
            # What the edit needs, before it moves anything.
            haction, pset = _capabilities(hwp, 'HStyle')
        list_id, para = _native_list(hwp, rows, progress)
        if paragraph_style:
            block, words = nf._paragraph_block(rows, list_id, para, before['model'])  # noqa: SLF001
        else:
            block, words = nf._character_block(rows, list_id, para)  # noqa: SLF001
        target = {'kind': target_kind, 'native_block': list(block),
                  'paragraphs': [{'paragraph_index': row['paragraph']['index'], 'context': row['paragraph']['context'], 'text': row['paragraph']['text'][:120]} for row in rows]}
        found = {'index': style['index'], 'name': style['name'], 'kind': style['kind'], 'hash': style['hash']}

        def report(after: Mapping[str, Any] | None) -> list[dict[str, Any]]:
            items = []
            for row in rows:
                number = numbers[row['paragraph']['index']]
                one = before['styles']['paragraphs'][number]
                two = (after or before)['styles']['paragraphs'][number]
                low, high = (0, len(one['atoms'])) if paragraph_style else (row['first'] or 0, row['last'] or 0)
                left, right = dict(one, atoms=one['atoms'][low:high]), dict(two, atoms=two['atoms'][low:high])
                items.append({'paragraph_index': one['census_index'], 'context': one['context'], 'text': one['text'][:120],
                              'style_before': before['styles']['styles'][one['style']]['name'], 'style_after': (after or before)['styles']['styles'][two['style']]['name'],
                              **({'characters': outcomes.get(number, 'unchanged')} if paragraph_style else {}),
                              'before': _report_paragraph(left, before['styles']), 'after': _report_paragraph(right, (after or before)['styles'])})
            return items

        if already:
            _enter(progress)
            _place(hwp, (list_id, para, block[2]))
            nf._require_page(page_of(), request['expected_page'])  # noqa: SLF001
            return {'style': found, 'target': target, 'result': {'changed': False, 'status': 'already_set', 'paragraphs': report(None), 'native_actions': []},
                    'undo': _undo(False, before, None)}
        _select(hwp, block, words, page_of, request['expected_page'], progress)
        _defaults(haction, 'Style', pset)
        _write(pset, {'Apply': style['index']})
        _execute(haction, 'Style', pset, reach)
        after = read()
        link = _link(before['styles']['styles'], after['styles']['styles'])
        problems = _problems(before, after, link, targets=targets, styles=_unchanged_styles, lists=_DEFINITION_LISTS, removals_only=True)
        if problems:
            raise _not_as_requested('style apply', 'style_apply_exact', problems, link, before['styles']['styles'], after['styles']['styles'])
        _leave(hwp, restore)
        return {'style': found, 'target': target,
                'result': {'changed': True, 'status': 'applied', 'paragraphs': report(after), 'native_actions': ['Execute:Style']},
                'undo': _undo(True, before, after)}

    return _guarded(reach, 'style apply', body)


# Creating a style.

def create_style(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], restore: tuple[int, int, int],
                 on_mutating: Callable[[], None] | None = None, progress: dict[str, Any] | None = None) -> dict[str, Any]:
    """One StyleAddNew of the legacy action family from a paragraph that shows the base style, proven to add exactly one style of the
    requested name at whatever index Hancom gives it, every old style kept by its identity wherever it now is."""
    reach = _MutationReach(on_mutating)
    kind = request['kind']
    character, paragraph = request['character'], request['paragraph']

    def body() -> dict[str, Any]:
        before = read()
        model = before['styles']
        _require_identities(model)
        if kind != 'paragraph':
            raise NativeNumberingRefusal("creating a character style is not shown: Hancom's StyleAddNew example creates a style without naming a kind; "
                                         'nothing was written', code='style_kind_unsupported')
        if paragraph:
            raise NativeNumberingRefusal(f"paragraph properties ({', '.join(sorted(paragraph))}) for a new style are not shown by Hancom's StyleAddNew "
                                         'example, which sets only the character shape; create the style without them; nothing was written',
                                         code='style_variant_unsupported')
        _require_free(model, request['name'])
        based = _find(model, request['based_on'])
        if based['kind'] != 'paragraph':
            raise NativeNumberingRefusal('a new style is based on a paragraph style, whose definitions a paragraph shows', code='style_kind_unsupported')
        if based['hash'] != request['expected_based_on_hash']:
            raise NativeNumberingRefusal('the base style is not as style_inventory reported it; read the inventory again', code='style_hash_mismatch')
        if model['styles_hash'] != request['expected_styles_hash']:
            raise NativeNumberingRefusal('the style list is not as style_inventory reported it; read the inventory again', code='styles_hash_mismatch')
        successor = None
        if request['next_style'] is not None:
            successor = _find(model, request['next_style'])
            if successor['kind'] != 'paragraph':
                raise NativeNumberingRefusal('next_style names a character style; the next paragraph takes a paragraph style', code='style_kind_unsupported')
        # The set's Next names the next style in the list as StyleAddNew leaves it (_planned_next); the proof still compares the next
        # style the new style names by its identity, wherever the action puts either of them.
        following = _planned_next(based, successor)
        if 'font_family' in character:
            nf._require_font(character['font_family'], before['model'])  # noqa: SLF001
        caret = _stand(before, based, 'the base style')
        methods, pset, calls = _legacy(hwp, _CREATE_ACTION, ('SetItem', 'ItemExist', 'Item') if character else ('SetItem',))
        _caret_at(hwp, caret, progress)
        _legacy_call('get_default.call', lambda: methods['GetDefault'](pset), _get_default_answer(_CREATE_ACTION), f'{_CREATE_ACTION} GetDefault')
        _set_items(calls['SetItem'], {'NameLocal': request['name'], 'NameEng': request['english_name'], 'Next': following}, _CREATE_ACTION, 'set_item')
        if character:
            _filled_char_shape(calls, _CREATE_ACTION, character,
                               f"the native {_CREATE_ACTION} defaults hold no character shape of the base style to change, and a sub-set "
                               "made empty would not carry the base's other character properties; create the style without character "
                               'properties and change them with style_update_exact; nothing was written')
        _legacy_execute(methods['Execute'], pset, _CREATE_ACTION, reach)
        after = read()
        link = _link(model['styles'], after['styles']['styles'], request['name'])
        problems = _problems(before, after, link, targets={}, styles=_create_check(based, request, model['fonts_by_name']),
                             lists=_DEFINITION_LISTS, removals_only=False)
        if problems:
            raise _not_as_requested('style create', 'style_create_exact', problems, link, model['styles'], after['styles']['styles'])
        _leave(hwp, restore)
        new = link['added']
        return {'style': public_style(after['styles'], new), 'based_on': {'index': based['index'], 'name': based['name'], 'hash': based['hash']},
                'requested': {'character': dict(character), 'paragraph': dict(paragraph)},
                'result': {'changed': True, 'status': 'created', 'native_actions': [f'Execute:{_CREATE_ACTION}'], 'pattern_paragraph': caret[1]},
                'next_binding': {'style_name': new['name'], 'expected_style_hash': new['hash'], 'expected_styles_hash': after['styles']['styles_hash']},
                'undo': _undo(True, before, after)}

    return _guarded(reach, 'style create', body)


def _planned_next(based: Mapping[str, Any], successor: Mapping[str, Any] | None) -> int:
    """The set's Next for the list as StyleAddNew leaves it, from the base the caret stands in and the requested next style, both bound
    in the list read before the action.

    C04 measured it natively (Source07, Hancom 13.0.0.3457, one host, based on 바탕글 at index 0): the new style was inserted right
    after the base, at index 1; every old style, its next style included, and every paragraph's style kept its identity; and the
    set's Next 1, 본문's index before the action, resolved to the new style itself, as an index of the list after the insertion
    kept as written would (the stored number was not read separately). So the new style's slot is the base's index plus one: without a next style it names that slot, itself; a
    requested next style before the slot keeps its index, and one at or after it is one further. Other bases and hosts are not
    measured. Wherever the action puts the new style instead, the planned Next names another style, or none, and the identity proof
    holds the edit (new_style_wrong, next_style_dangling)."""
    slot = based['index'] + 1
    if successor is None:
        return slot
    return successor['index'] if successor['index'] < slot else successor['index'] + 1


def _font_problems(later: Mapping[str, Any], base: Mapping[str, Any], wanted: Mapping[str, Any], known: Mapping[tuple[str, str], set[str]], what: str) -> list[str]:
    """The fonts a new or changed style names, by their whole content. Each language keeps the base's font unless font_family was
    requested; a requested font the document already lists must be that very font; only a font the document did not list is new."""
    if set(later['char_fonts']) != set(base['char_fonts']):
        return [f'{what} names fonts for other languages']
    problems = []
    for language, digest in later['char_fonts'].items():
        if 'font_family' not in wanted:
            if digest != base['char_fonts'][language]:
                problems.append(f"{what}'s {language} font is not the base style's font")
        elif known.get((language, wanted['font_family'])) is not None and digest not in known[(language, wanted['font_family'])]:
            problems.append(f"{what}'s {language} font is not the document's own {wanted['font_family']}")
    return problems


def _create_check(based: Mapping[str, Any], request: Mapping[str, Any], known: Mapping[tuple[str, str], set[str]]
                  ) -> Callable[[list[dict[str, Any]], list[dict[str, Any]], Mapping[str, Any]], list[dict[str, Any]]]:
    """Every old style kept (_unchanged_styles), and the one new style: the requested kind, English name and lock, the base's language,
    the base's definitions with the requested properties, the base's fonts or the requested one, and as next style the requested
    paragraph style (itself when none was requested), by identity."""
    def check(old: list[dict[str, Any]], new: list[dict[str, Any]], link: Mapping[str, Any]) -> list[dict[str, Any]]:
        problems = _unchanged_styles(old, new, link)
        added = link['added']
        if added is None:
            return problems
        kind = request['kind']
        fields: list[str] = []
        texts: list[str] = []
        expected = found = None
        expected_char = nf._char_after(based['char'], request['character'])  # noqa: SLF001
        if added['kind'] != kind:
            fields.append('kind')
            texts.append(f"the new style {added['name']} is a {added['kind']} style, not a {kind} style")
        else:
            if added['english_name'] != request['english_name']:
                fields.append('english_name')
                texts.append("the new style's English name is not the requested one")
            if added['lock'] is not False:
                fields.append('lock')
                texts.append('the new style is locked')
            if _settings(added).get('langid') != _settings(based).get('langid'):
                fields.append('language')
                texts.append("the new style's language is not the base style's")
            if not nf._same_character(added['char'], expected_char, request['character']):  # noqa: SLF001
                fields.append('character')
                texts.append("the new style's character shape is not the base style's with the requested properties")
            if kind == 'paragraph' and added['para'] != nf._para_after(based['para'], request['paragraph']):  # noqa: SLF001
                fields.append('paragraph')
                texts.append("the new style's paragraph shape is not the base style's with the requested properties")
            fonts = _font_problems(added, based, request['character'], known, 'the new style')
            if fonts:
                fields.append('fonts')
                texts += fonts
            wanted = request['next_style'] if request['next_style'] is not None else request['name']
            target = _next_of(added, new)
            if target is None:
                problems.append(_fact('next_style_dangling', f"the new style {added['name']} names a next style outside the style list",
                                      name=_named(added['name']), new_index=added['index']))
            elif _identity(target) != (wanted, 'paragraph'):
                fields.append('next')
                expected, found = wanted, target['name']
                texts.append(f"the new style's next style is {target['name']}, not {wanted}")
        if fields:
            shape = {}
            if 'character' in fields or 'fonts' in fields:
                shape = _shape_facts(added['char'], expected_char, request['character'], added['char_fonts'],
                                     None if 'font_family' in request['character'] else based['char_fonts'])
            problems.append(_fact('new_style_wrong', '; '.join(texts), name=_named(added['name']), new_index=added['index'], fields=fields,
                                  expected=_named(expected) if expected else None, found=_named(found) if found else None, **shape))
        return problems

    return check


# Changing a style.

def update_style(hwp: Any, *, request: Mapping[str, Any], read: Callable[[], Mapping[str, Any]], restore: tuple[int, int, int],
                 on_mutating: Callable[[], None] | None = None, progress: dict[str, Any] | None = None) -> dict[str, Any]:
    """One StyleDirectEdit of the legacy action family, NameLocal naming the style and CharShape the style's whole shape read at its plain
    use with the requested items changed, proven on the style and every use."""
    reach = _MutationReach(on_mutating)
    character, paragraph = request['character'], request['paragraph']

    def body() -> dict[str, Any]:
        before = read()
        model = before['styles']
        _require_identities(model)
        style = _bound(model, request['style_name'], request['expected_style_hash'])
        if style['kind'] != 'paragraph':
            raise NativeNumberingRefusal("changing a character style is not shown: Hancom's StyleDirectEdit example changes a paragraph style; "
                                         'nothing was written', code='style_kind_unsupported')
        if request['new_name'] is not None or request['new_english_name'] is not None:
            raise NativeNumberingRefusal("a rename has no documented mechanism: StyleDirectEdit's NameLocal selects the style to change and is never "
                                         "written as a new name; change the style's properties only; nothing was written", code='style_rename_unsupported')
        if paragraph:
            raise NativeNumberingRefusal(f"paragraph properties ({', '.join(sorted(paragraph))}) of a style are not shown by Hancom's StyleDirectEdit "
                                         'example, which changes only the character shape; nothing was written', code='style_variant_unsupported')
        uses = _uses(model, style['index'])
        if len(uses) != request['expected_uses'] or _uses_hash(uses) != request['expected_uses_hash']:
            raise NativeNumberingRefusal('the paragraphs using the style are not as style_inventory reported them; read the inventory again',
                                         code='style_uses_mismatch')
        if uses and request['all_uses'] is not True:
            raise NativeNumberingRefusal(f'the style is used by {len(uses)} paragraph(s), and a change of the style changes them all; give all_uses=true',
                                         code='style_uses_unconfirmed')
        if len(uses) > USE_ROWS_ONE:
            raise NativeNumberingRefusal(f'the style is used by more than {USE_ROWS_ONE} paragraphs', code='style_uses_too_many')
        own = [use for use in uses if not use['plain']]
        if own:
            raise NativeNumberingRefusal(f'{len(own)} paragraph(s) using the style have a format of their own (first: {_where(own[0])}); '
                                         'how a style change reaches them is not measured', code='style_use_has_own_format')
        expected_char = nf._char_after(style['char'], character)  # noqa: SLF001
        if nf._same_character(style['char'], expected_char, character):  # noqa: SLF001
            return _update_report(before, None, style, uses, request, changed=False)
        if 'font_family' in character:
            nf._require_font(character['font_family'], before['model'])  # noqa: SLF001
        # The caret stands in a plain use of the style, so the shape read there and whatever GetDefault fills the set's other items with
        # are the style's own. C06 found StyleDirectEdit's defaults without a CharShape sub-set: the change writes the whole shape read
        # at the use instead, with only the requested items changed in it, and set back on the set (topic 2354 post 5).
        caret = _stand(before, style, 'the style')
        _caret_at(hwp, caret, progress)
        shape, write_shape = _style_char_shape(hwp, style)
        _set_items(write_shape, nf._character_items(character), _CHANGE_ACTION, 'subset_set_item')  # noqa: SLF001
        methods, pset, calls = _legacy(hwp, _CHANGE_ACTION, ('SetItem',), entered=True)
        _legacy_call('get_default.call', lambda: methods['GetDefault'](pset), _get_default_answer(_CHANGE_ACTION), f'{_CHANGE_ACTION} GetDefault')
        _set_items(calls['SetItem'], {'NameLocal': style['name']}, _CHANGE_ACTION, 'set_item')
        _set_items(calls['SetItem'], {'CharShape': shape}, _CHANGE_ACTION, 'set_item')
        _legacy_execute(methods['Execute'], pset, _CHANGE_ACTION, reach)
        after = read()
        changed: dict[str, Any] = {}
        link = _link(model['styles'], after['styles']['styles'])
        problems = _problems(before, after, link, targets={use['number']: _use_check(after, style) for use in uses},
                             styles=_update_check(style, style['name'], None, character, style['para'], changed, model['fonts_by_name']),
                             lists=_DEFINITION_LISTS, removals_only=False)
        if problems:
            raise _not_as_requested('style update', 'style_update_exact', problems, link, model['styles'], after['styles']['styles'])
        _leave(hwp, restore)
        return _update_report(before, after, style, uses, request, changed=True, caret=caret, later_index=link['map'][style['index']])

    return _guarded(reach, 'style update', body)


def _use_check(after: Mapping[str, Any], style: Mapping[str, Any]) -> Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], list[str]]:
    def check(one: Mapping[str, Any], two: Mapping[str, Any], link: Mapping[str, Any]) -> list[str]:
        where = _where(one)
        found = link['map'].get(style['index'])
        if found is None:
            return [f'{where}: the style is gone']
        new = after['styles']['styles'][found]
        problems = []
        if not _kept(one, two, inst=False) or two['style'] != found:
            problems.append(f'{where} changed more than its format')
        if two['para_sha'] != new['para_sha256'] or any(shape != new['char_sha256'] for shape in _shapes(two)) \
                or (one['end_sha'] is not None and two['end_sha'] != new['char_sha256']):
            problems.append(f"{where} does not show the style's new definitions")
        return problems

    return check


def _update_check(style: Mapping[str, Any], name: str, english: str | None, character: Mapping[str, Any], expected_para: Mapping[str, Any],
                  changed: dict[str, Any], known: Mapping[tuple[str, str], set[str]]
                  ) -> Callable[[list[dict[str, Any]], list[dict[str, Any]], Mapping[str, Any]], list[dict[str, Any]]]:
    """Every other style kept (_unchanged_styles), and the changed style wherever it now is: its settings and names, its next style by
    identity, its paragraph shape, and its character shape the old one with the requested properties."""
    def check(old: list[dict[str, Any]], new: list[dict[str, Any]], link: Mapping[str, Any]) -> list[dict[str, Any]]:
        problems = _unchanged_styles(old, new, link, skip=style['index'])
        found = link['map'].get(style['index'])
        if found is None:
            return problems
        later = new[found]
        wanted = {**_settings(style), 'name': name}
        if english is not None:
            wanted['engname'] = english
        theirs = _settings(later)
        attributes = sorted(key for key in set(wanted) | set(theirs) if wanted.get(key) != theirs.get(key))
        fields: list[str] = []
        texts: list[str] = []
        expected = now_name = None
        if attributes:
            fields.append('settings')
            texts.append('the style does not have the requested names, or another of its settings changed')
        following, now = _next_of(style, old), _next_of(later, new)
        if now is None:
            problems.append(_fact('next_style_dangling', f"the style {later['name']} names a next style outside the style list", name=_named(later['name']),
                                  new_index=found))
        elif following is not None and _identity(following) != _identity(now):
            fields.append('next')
            expected, now_name = following['name'], now['name']
            texts.append(f"the style's next style is {now['name']}, not {following['name']}")
        expected_char = nf._char_after(style['char'], character)  # noqa: SLF001
        if not nf._same_character(later['char'], expected_char, character):  # noqa: SLF001
            fields.append('character')
            texts.append("the style's character shape is not the old one with the requested properties")
        if later['para'] != expected_para:
            fields.append('paragraph')
            texts.append("the style's paragraph shape is not the old one with the requested properties")
        fonts = _font_problems(later, style, character, known, 'the style')
        if fonts:
            fields.append('fonts')
            texts += fonts
        if fields:
            shape = {}
            if 'character' in fields or 'fonts' in fields:
                shape = _shape_facts(later['char'], expected_char, character, later['char_fonts'], None if 'font_family' in character else style['char_fonts'])
            problems.append(_fact('changed_style_wrong', '; '.join(texts), name=_named(style['name']), old_index=style['index'], new_index=found, fields=fields,
                                  attributes=[key for key in attributes if _TOKEN.fullmatch(key)][:16] or None,
                                  expected=_named(expected) if expected else None, found=_named(now_name) if now_name else None, **shape))
        changed['style'] = later
        return problems

    return check


def _update_report(before: Mapping[str, Any], after: Mapping[str, Any] | None, style: Mapping[str, Any], uses: list[dict[str, Any]],
                   request: Mapping[str, Any], *, changed: bool, caret: tuple[int, int, int] | None = None, later_index: int | None = None) -> dict[str, Any]:
    side = after or before
    index = style['index'] if later_index is None else later_index
    later = side['styles']['styles'][index]
    rows = []
    for use in uses[:USE_ROWS_ONE]:
        two = side['styles']['paragraphs'][use['number']]
        rows.append({**_public_use(use), 'before': _report_paragraph(use, before['styles']), 'after': _report_paragraph(two, side['styles'])})
    later_uses = _uses(side['styles'], index)
    return {'style_before': public_style(before['styles'], style, rows=0), 'style_after': public_style(side['styles'], later, rows=0),
            'requested': {'character': dict(request['character']), 'paragraph': dict(request['paragraph']),
                          'new_name': request['new_name'], 'new_english_name': request['new_english_name']},
            'result': {'changed': changed, 'status': 'updated' if changed else 'already_set',
                       'native_actions': [f'Execute:{_CHANGE_ACTION}'] if changed else [], 'pattern_paragraph': caret[1] if caret else None,
                       'affected': {'total': len(uses), 'by_context': {context: sum(1 for use in uses if use['context'] == context) for context in CONTEXTS},
                                    'rows': rows}},
            'next_binding': {'style_name': later['name'], 'expected_style_hash': later['hash'], 'expected_uses': len(later_uses),
                             'expected_uses_hash': _uses_hash(later_uses)},
            'undo': _undo(changed, before, after)}
