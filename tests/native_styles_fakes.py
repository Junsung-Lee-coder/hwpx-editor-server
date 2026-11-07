"""A fake SDK for the named style commands, on top of the header and footer fake.

The document has a style list and each paragraph a style index (``P/@Style``), with the attribute names and values of
actual Hancom 13.0.0.3457 HWPML2X captures in this repository (``STYLE`` with CharShape, EngName, Id, LangId, LockForm,
Name, NextStyle, ParaShape for a paragraph style only, and Type ``Para`` or ``Char``; a run holds only its CharShape).

The style actions follow the official references:
- Apply: ``HAction.GetDefault``/``Execute`` return BOOL (HwpAutomation_2504); ``Style`` takes ``HParameterSet.HStyle`` with
  ``Apply``, the style index. Style on a paragraph gives every touched paragraph the style's paragraph and character shapes
  (``apply_model`` 'reset'), or only to the characters that showed the old style's character shape ('kept_direct').
- Create and change: the legacy action family, as Hancom's own forum examples use it (forum.developer.hancom.com topic 2505
  post 2 for StyleAddNew, topic 1552 post 4 for StyleDirectEdit), with the answers the installed type library declares
  (HwpObject.tlb of Hancom 13.0.0.3457, sha256 047f64bd...): ``CreateAction(name)`` gives an IDHwpAction whose
  ``CreateSet()`` gives an IDHwpParameterSet; ``GetDefault(set)`` is declared VT_I4 and answers ``legacy_defaults_result``,
  by default the exact int 1 it answered natively for both actions (GD01 StyleAddNew, GD02 StyleDirectEdit, 2026-11-06,
  each on its own fresh instance; what 1 means was not established); ``SetItem`` is VT_VOID (None); ``ItemExist`` is
  VT_BOOL; ``Item`` gives an item's value (0 when absent); ``CreateItemSet`` makes a new sub-set held as that item.
  ``Execute(set)`` is VT_BOOL and answers ``execute_result``: True, as the same IDHwpAction.Execute answered natively for
  this repository's InsertText, CharShape and ParagraphShape commands. HStyleItem is not offered (forum topic 1650 post 2:
  no API handles it from outside), and HAction refuses the create and change actions, so a product that mixes the two
  families fails here.
- Sub-sets are copies by default, as Hancom staff state for Hangul Automation (topic 2354 post 5, 2025-03-27): ``Item``
  and ``CreateItemSet`` give a detached copy, and a change reaches the parent only when the sub-set is set back with
  ``SetItem``. ``legacy_copies`` False is HwpCtrl's reference behaviour from the same post, where setting back is harmless.
How Hancom fills these sets and what each action does to the styles and paragraphs is not measured; this fake makes one
consistent choice, documented on each method, and adversarial knobs for the others:
- GetDefault of StyleAddNew and StyleDirectEdit fills the set from the caret paragraph's style (Type, NameLocal, NameEng,
  Next, LockForm) and, with ``legacy_subsets``, the CharShape and ParaShape sub-sets from the caret's shapes, except that
  StyleDirectEdit's leaves no CharShape sub-set by default: C06 (Source09, Hancom 13.0.0.3457, one host, 바탕글 of the seed)
  measured ItemExist("CharShape") False after its GetDefault and NameLocal, by the source path. ``direct_edit_char_shape``
  True gives the filled one Source09 modelled;
- ``CharShape``, IHwpObject.CharShape (HwpAutomation_2504 page 12; DISPID 8, VT_DISPATCH, get and put in gen_py f9ed176b):
  the character shape of the selection, or at the caret without one, as a CharShape parameter set (ParameterSetTable_2504 set
  13) holding every one of its items (CHAR_SHAPE_ITEMS); an item whose value differs within the selection is absent. Each read
  gives a new set (``char_shape_copies``; False gives one object every time). Writing into the set changes nothing in the
  document; a put would, and is recorded in ``char_shape_puts``. Knobs give a partial, foreign or other-spelled set;
- a sub-set made with ``CreateItemSet`` starts from a factory shape (``factory_char``), not from the caret's or the style's: Hancom's
  answer in forum topic 1044 post 6 (2023-11-09, a PageBorder FillAttr example) says a sub-set made that way needs every one of its
  child items set, and a partial change edits the sub-set GetDefault filled, which Item gives. A model of that mechanism, not a
  measured after-state;
- StyleAddNew inserts a style with the set's names and next style, the caret's paragraph shape and the CharShape sub-set's items
  over the shape that sub-set started from, right after the style the caret paragraph shows, as C04 measured it natively on Source07 (based on 바탕글 at index 0, the new style at
  index 1). The insertion renumbers every old style's NextStyle and every paragraph's P/@Style, so each keeps its identity, as all
  23 old styles did in C04; the new style keeps the set's Next as written, an index of the list after the insertion, which C04's
  Next 1 resolving to the new style itself shows. ``add_at`` puts it elsewhere, an unexpected boundary;
- StyleDirectEdit changes the style NameLocal names: its character shape becomes the CharShape sub-set's items over the shape that
  sub-set started from, and it takes the set's NameEng, Next and LockForm; every paragraph showing exactly its old definitions
  takes the new ones. A set read from ``CharShape`` starts from the factory shape, like a made one: a whole set gives its own shape,
  and a partial one resets what it lacks (a model, not measured).
Not Hancom evidence.
"""
from __future__ import annotations

import copy
import re
from typing import Any
from xml.sax.saxutils import escape

from tests.native_format_fakes import _INLINE, _PICTURE, _SECTION_HEAD, _TABLE, _LANGS, char, para_length, paragraph, shape, table, width
from tests.native_header_footer_fakes import HeaderFooterHwp, area
from tests.native_text_fakes import _Params, _TextHAction

_UNDERLINE = {None: 0, 'Bottom': 1, 'Top': 3}
_UNDERLINE_BACK = {0: None, 1: 'Bottom', 3: 'Top'}
_ALIGN = ('Justify', 'Left', 'Right', 'Center', 'Distribute', 'DistributeSpace')
_SPACING = ('Percent', 'Fixed', 'BetweenLines', 'AtLeast')
_HEADING = ('None', 'Outline', 'Number', 'Bullet')


def style(name: str, *, eng: str | None = None, kind: str = 'Para', chars: dict[str, Any] | None = None, para: dict[str, Any] | None = None,
          next_index: int | None = None, lock: int = 0) -> dict[str, Any]:
    return {'name': name, 'eng': eng, 'type': kind, 'char': dict(chars or char()), 'para': (dict(para or shape()) if kind == 'Para' else None),
            'next': next_index, 'lock': lock}


def styled(text_or_runs: Any, index: int, *, para_shape: dict[str, Any] | None = None) -> dict[str, Any]:
    """A paragraph with style ``index``; its runs are given as for paragraph()."""
    runs = text_or_runs if isinstance(text_or_runs, tuple) else (text_or_runs,)
    item = paragraph(*runs, para_shape=para_shape)
    item['style'] = index
    return item


def default_styles() -> list[dict[str, Any]]:
    """바탕글, 본문, 개요 1 and two character styles, as the built-in Hancom list starts, with distinct definitions."""
    return [
        style('바탕글', eng='Normal', next_index=0),
        style('본문', eng='Body', para=shape(Left=3000), next_index=1),
        style('개요 1', eng='Outline 1', chars=char(Height=1200, Bold=True), para=shape(HeadingType='Outline', Level=0), next_index=2),
        style('강조', eng='Emphasis', kind='Char', chars=char(Bold=True, TextColor=255), next_index=0),
        style('쪽 번호', eng='Page Number', kind='Char', chars=char(Height=900), next_index=0),
    ]


class _Typed:
    """A typed parameter set object (HParameterSet.<name>): its items are attributes, ``HSet`` is the set itself.

    An item GetDefault did not fill raises AttributeError when read, so an engine reading what it never asked for shows."""

    def __init__(self, set_id: str, subsets: tuple[str, ...] = ()) -> None:
        object.__setattr__(self, 'set_id', set_id)
        object.__setattr__(self, 'items', {})
        object.__setattr__(self, 'writes', [])
        object.__setattr__(self, 'base', None)
        object.__setattr__(self, 'subsets', subsets)
        object.__setattr__(self, 'HSet', self)
        for name in subsets:
            self.items[name] = _Typed(name)

    def __getattr__(self, name: str) -> Any:
        items = object.__getattribute__(self, 'items')
        if name in items:
            return items[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.subsets:
            raise AttributeError(f'{name} is a sub-set')
        self.items[name] = value
        self.writes.append((name, value))

    def reset(self) -> None:
        for name in list(self.items):
            if name not in self.subsets:
                del self.items[name]
        self.writes.clear()
        for name in self.subsets:
            self.items[name].reset()


class _StyleParams(_Params):
    def __init__(self) -> None:
        super().__init__()
        self.HStyle = _Typed('Style')


STYLE_ACTIONS = ('Style', 'StyleAddNew', 'StyleDirectEdit')
# Style actions HAction never takes here: the create and change actions belong to the legacy family.
_NOT_HACTION = frozenset({'StyleAdd', 'StyleAddNew', 'StyleDirectEdit', 'StyleChangeToCurrentShape', 'StyleEdit', 'StyleEx'})


class _StyleHAction(_TextHAction):
    def GetDefault(self, name: str, hset: Any) -> Any:  # noqa: N802
        if name in _NOT_HACTION:
            raise AssertionError(f'{name} is not taken through HAction here')
        if name != 'Style':
            return super().GetDefault(name, hset)
        self.hwp.calls.append(('GetDefault', name, getattr(hset, 'set_id', None)))
        return self.hwp.style_defaults(name, hset)

    def Execute(self, name: str, hset: Any) -> Any:  # noqa: N802
        if name in _NOT_HACTION:
            raise AssertionError(f'{name} is not taken through HAction here')
        if name != 'Style':
            return super().Execute(name, hset)
        return self.hwp.style_execute(name, hset)


class _LegacySet:
    """IDHwpParameterSet (HwpAutomation_2504 pages 62-64). With ``copies`` (Hangul Automation, topic 2354 post 5) a sub-set that
    Item or CreateItemSet gives, and one SetItem stores, is a deep copy; without it (HwpCtrl) it is the very object."""

    def __init__(self, set_id: str, copies: bool = True) -> None:
        self.set_id, self.copies = set_id, copies
        self.items: dict[str, Any] = {}
        self.base: dict[str, Any] | None = None  # The shape GetDefault read this sub-set from.

    def _detached(self, value: Any) -> Any:
        return copy.deepcopy(value) if self.copies and isinstance(value, _LegacySet) else value

    def SetItem(self, name: str, value: Any) -> None:  # noqa: N802
        self.items[name] = self._detached(value)

    def Item(self, name: str) -> Any:  # noqa: N802
        return self._detached(self.items.get(name, 0))

    def ItemExist(self, name: str) -> bool:  # noqa: N802
        return name in self.items

    def CreateItemSet(self, name: str, set_id: str) -> '_LegacySet':  # noqa: N802
        made = _LegacySet(set_id, self.copies)
        self.items[name] = made
        return self._detached(made)


class _LegacyStyleAction:
    """IDHwpAction (HwpAutomation_2504 page 61) for StyleAddNew and StyleDirectEdit."""

    def __init__(self, hwp: 'StyleHwp', name: str) -> None:
        self.hwp, self.name = hwp, name

    def CreateSet(self) -> _LegacySet:  # noqa: N802
        return _LegacySet('StyleItem', self.hwp.legacy_copies)

    def GetDefault(self, pset: _LegacySet) -> Any:  # noqa: N802
        self.hwp.calls.append(('GetDefault', self.name, 'legacy'))
        return self.hwp.legacy_defaults(self.name, pset)

    def Execute(self, pset: _LegacySet) -> Any:  # noqa: N802
        return self.hwp.legacy_execute(self.name, pset)


def _char_items(props: dict[str, Any]) -> dict[str, Any]:
    items: dict[str, Any] = {'Height': props['Height'], 'TextColor': props['TextColor'], 'Bold': int(props['Bold']), 'Italic': int(props['Italic']),
                             'UnderlineType': _UNDERLINE[props['Underline']]}
    for lang in _LANGS:
        items[f'FaceName{lang}'] = (props.get('Fonts') or {}).get(lang, props['Font'])
        items[f'FontType{lang}'] = 1
    return items


# A character shape may carry more than the formatting fake's: a font, ratio, spacing, relative size and offset per language, and the
# shade colour, kerning, font space and symbol mark, as actual captures carry them. Absent, the formatting fake's values stand.
RICH_KEYS = ('Fonts', 'Ratios', 'Spacings', 'RelSizes', 'Offsets', 'ShadeColor', 'UseKerning', 'UseFontSpace', 'SymMark')

# The items of the CharShape parameter set: ParameterSetTable_2504 set 13, the same 67 as HCharShape of the installed type library
# (gen_py f9ed176b), which spells OutlineType as OutLineType. This fake uses the type library's spelling (``char_shape_spelling``).
CHAR_SHAPE_ITEMS = tuple(f'{kind}{lang}' for kind in ('FaceName', 'FontType', 'Size', 'Ratio', 'Spacing', 'Offset') for lang in _LANGS) + (
    'Bold', 'Italic', 'SmallCaps', 'Emboss', 'Engrave', 'SuperScript', 'SubScript', 'UnderlineType', 'OutLineType', 'ShadowType', 'TextColor',
    'ShadeColor', 'UnderlineShape', 'UnderlineColor', 'ShadowOffsetX', 'ShadowOffsetY', 'ShadowColor', 'StrikeOutType', 'DiacSymMark',
    'UseFontSpace', 'UseKerning', 'Height', 'StrikeOutShape', 'StrikeOutColor', 'BorderFill')
# Items no shape of this fake sets: their one value. A set that gives another is outside the fake.
_UNRENDERED = {'SmallCaps': 0, 'Emboss': 0, 'Engrave': 0, 'SuperScript': 0, 'SubScript': 0, 'OutLineType': 0, 'OutlineType': 0, 'ShadowType': 0,
               'UnderlineShape': 0, 'ShadowOffsetX': 0, 'ShadowOffsetY': 0, 'ShadowColor': 0, 'StrikeOutType': 0, 'StrikeOutShape': 0, 'StrikeOutColor': 0}
_WHOLE_ONLY = frozenset({'BorderFill', 'ShadeColor', 'DiacSymMark', 'UseKerning', 'UseFontSpace', 'UnderlineColor', *_UNRENDERED,
                         *(f'{kind}{lang}' for kind in ('Size', 'Ratio', 'Spacing', 'Offset') for lang in _LANGS)})


def char_shape_items(props: dict[str, Any]) -> dict[str, Any]:
    """Every item of the CharShape parameter set for a shape of this fake, as IHwpObject.CharShape gives them."""
    fonts = props.get('Fonts') or {}
    items: dict[str, Any] = {}
    for lang in _LANGS:
        items[f'FaceName{lang}'] = fonts.get(lang, props['Font'])
        items[f'FontType{lang}'] = 1
        items[f'Size{lang}'] = (props.get('RelSizes') or {}).get(lang, 100)
        items[f'Ratio{lang}'] = (props.get('Ratios') or {}).get(lang, props['Ratio'])
        items[f'Spacing{lang}'] = (props.get('Spacings') or {}).get(lang, 0)
        items[f'Offset{lang}'] = (props.get('Offsets') or {}).get(lang, 0)
    items.update(Bold=int(props['Bold']), Italic=int(props['Italic']), UnderlineType=_UNDERLINE[props['Underline']], TextColor=props['TextColor'],
                 ShadeColor=props.get('ShadeColor', 4294967295), UnderlineColor=props.get('UnderlineColor', 0), DiacSymMark=props.get('SymMark', 0),
                 UseFontSpace=int(bool(props.get('UseFontSpace', False))), UseKerning=int(bool(props.get('UseKerning', False))), Height=props['Height'])
    items.update({key: value for key, value in _UNRENDERED.items() if key != 'OutlineType'})
    items['BorderFill'] = _LegacySet('BorderFill')
    return items


def canonical_char(props: dict[str, Any]) -> dict[str, Any]:
    """One spelling for one shape: per-language values only where they differ from the shape's single value, defaults left out."""
    out = dict(props)
    fonts = {lang: (props.get('Fonts') or {}).get(lang, props['Font']) for lang in _LANGS}
    out.pop('Fonts', None)
    if len(set(fonts.values())) == 1:
        out['Font'] = fonts['Hangul']
    else:
        out['Fonts'], out['Font'] = fonts, fonts['Hangul']
    ratios = {lang: (props.get('Ratios') or {}).get(lang, props['Ratio']) for lang in _LANGS}
    out.pop('Ratios', None)
    if len(set(ratios.values())) == 1:
        out['Ratio'] = ratios['Hangul']
    else:
        out['Ratio'] = 100
        out['Ratios'] = {lang: value for lang, value in ratios.items() if value != 100}
    for key, default in (('Spacings', 0), ('RelSizes', 100), ('Offsets', 0)):
        values = {lang: value for lang, value in (props.get(key) or {}).items() if value != default}
        out.pop(key, None)
        if values:
            out[key] = values
    for key, default in (('ShadeColor', 4294967295), ('UseKerning', False), ('UseFontSpace', False), ('SymMark', 0), ('UnderlineColor', 0)):
        if key in out and out[key] == default:
            del out[key]
    return out


def _para_items(props: dict[str, Any]) -> dict[str, Any]:
    return {'AlignType': _ALIGN.index(props['Align']), 'LeftMargin': props['Left'], 'RightMargin': props['Right'], 'Indentation': props['Indent'],
            'LineSpacingType': _SPACING.index(props['LineSpacingType']), 'LineSpacing': props['LineSpacing'],
            'HeadingType': _HEADING.index(props['HeadingType']), 'Level': props['Level']}


class StyleHwp(HeaderFooterHwp):
    def __init__(self, body: list[dict[str, Any]], *, styles: list[dict[str, Any]] | None = None, areas: list[dict[str, Any]] | None = None,
                 **options: Any) -> None:
        self.styles = copy.deepcopy(styles if styles is not None else default_styles())
        for number, item in enumerate(self.styles):
            if item['next'] is None:
                item['next'] = number
        super().__init__(body, areas=list(areas or []), **options)
        self.HAction = _StyleHAction(self)
        self.HParameterSet = _StyleParams()
        self.apply_model = 'reset'
        self.defaults_result: Any = True
        self.legacy_defaults_result: Any = 1  # What the legacy GetDefault answers: the int 1 GD01 and GD02 observed natively.
        self.legacy_subsets = True  # Whether the legacy GetDefault fills the CharShape and ParaShape sub-sets.
        # StyleDirectEdit's GetDefault leaves no CharShape sub-set, as C06 found natively; True fills it as for StyleAddNew.
        self.direct_edit_char_shape = False
        self.char_shape_copies = True  # Each IHwpObject.CharShape read gives a new set; False gives one object every time.
        self.char_shape_drop: tuple[str, ...] = ()  # Items the read set does not hold.
        self.char_shape_spelling = 'OutLineType'  # The read set's spelling of the outline item: the type library's, or 'OutlineType'.
        # The shape the read describes, from the caret's: a foreign, neighbouring or partly reset one (an adversary).
        self.char_shape_from: Any = None
        self.char_shape_puts: list[Any] = []  # Every IHwpObject.CharShape put: the product makes none.
        self._char_shape_object: Any = None
        # A wrong outcome: GetDefault leaves the sub-sets in the set, so ItemExist answers True, but their content is the factory shape's
        # (factory_char), not the caret's, like a sub-set made with CreateItemSet.
        self.legacy_subsets_unfilled = False
        self.legacy_copies = True  # Automation's copy sub-sets (topic 2354 post 5); False for HwpCtrl's references.
        # Where StyleAddNew puts the new style. None: right after the style of the caret paragraph, where C04 measured it (one host, a
        # base at index 0; other positions are not measured). Any other index is a boundary the product did not plan for. Either way
        # the insertion renumbers P/@Style of every paragraph (body, cells, objects, headers and footers) and NextStyle of every old
        # style, and the new style keeps the set's Next as written, an index of the list after the insertion (C04).
        self.add_at: int | None = None
        self.add_restyles_caret = False  # A wrong outcome: StyleAddNew also gives the caret paragraph the new style.
        self.add_renames_current = False  # A wrong outcome: StyleAddNew renames the caret paragraph's style instead.
        self.change_targets: int | None = None  # A wrong outcome: StyleDirectEdit changes this style, whatever NameLocal names.
        self.change_in_place_shared = False  # A wrong outcome: the change also reaches every style with the same old definitions.
        self.change_skips_uses = False  # A wrong outcome: the uses keep the old definitions.
        # The character shape a sub-set made with CreateItemSet starts from, not the style's (topic 1044 post 6, above). A model: which
        # shape Hancom starts from is not measured, and C03 kept no after-state.
        self.factory_char = char()
        self.next_inst = 700001
        # Font content beyond Name and Type, as actual captures carry it in a TYPEINFO child (tests/fixtures/native_fields): by font name, the
        # TYPEINFO attributes to write. Empty by default, so the text is the one these tests had before.
        self.font_info: dict[str, dict[str, str]] = {}
        self.fonts_first: list[str] = []  # Fonts Hancom numbers first, renumbering every other font (a renumbering, not a change).

    # The document text.
    def _shapes(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        chars, paras, fonts = super()._shapes()
        for item in self.styles:
            if item['char'] not in chars:
                chars.append(item['char'])
            if item['char']['Font'] not in fonts:
                fonts.append(item['char']['Font'])
            if item['para'] is not None and item['para'] not in paras:
                paras.append(item['para'])
        for item in chars:
            for name in (item.get('Fonts') or {}).values():
                if name not in fonts:
                    fonts.append(name)
        if self.fonts_first:
            fonts = [name for name in self.fonts_first if name in fonts] + [name for name in fonts if name not in self.fonts_first]
        return chars, paras, fonts

    def _char_xml(self, index: int, props: dict[str, Any], fonts: list[str]) -> str:
        if not any(key in props for key in RICH_KEYS):
            return super()._char_xml(index, props, fonts)

        def per(values: Any, default: Any) -> str:
            return ' '.join(f'{lang}="{values.get(lang, default) if isinstance(values, dict) else values}"' for lang in sorted(_LANGS))

        ids = ' '.join(f'{lang}="{fonts.index((props.get("Fonts") or {}).get(lang, props["Font"]))}"' for lang in sorted(_LANGS))
        out = (f'<CHARSHAPE BorderFillId="2" Height="{props["Height"]}" Id="{index}" ShadeColor="{props.get("ShadeColor", 4294967295)}" '
               f'SymMark="{props.get("SymMark", 0)}" TextColor="{props["TextColor"]}" UseFontSpace="{str(props.get("UseFontSpace", False)).lower()}" '
               f'UseKerning="{str(props.get("UseKerning", False)).lower()}"><FONTID {ids}/><RATIO {per(props.get("Ratios", props["Ratio"]), props["Ratio"])}/>'
               f'<CHARSPACING {per(props.get("Spacings", 0), 0)}/><RELSIZE {per(props.get("RelSizes", 100), 100)}/><CHAROFFSET {per(props.get("Offsets", 0), 0)}/>')
        if props['Italic']:
            out += '<ITALIC/>'
        if props['Bold']:
            out += '<BOLD/>'
        if props['Underline']:
            out += f'<UNDERLINE Color="{props.get("UnderlineColor", 0)}" Shape="Solid" Type="{props["Underline"]}"/>'
        return out + '</CHARSHAPE>'

    @property
    def CharShape(self) -> Any:  # noqa: N802
        """IHwpObject.CharShape: the selection's character shape, or the caret's without one, as a CharShape parameter set."""
        self.calls.append(('CharShape', 'get'))
        if not self.char_shape_copies and self._char_shape_object is not None:
            return self._char_shape_object
        if self.selection is not None:
            every = [char_shape_items(atom['cs']) for _para, atoms in self._selected_atoms() for atom in atoms]
            items = {key: value for key, value in every[0].items() if all(other.get(key) == value for other in every[1:]) or key == 'BorderFill'} if every else {}
        else:
            source = self._caret_char()
            if self.char_shape_from is not None:
                source = self.char_shape_from(self, source)
            items = char_shape_items(source)
        for key in self.char_shape_drop:
            items.pop(key, None)
        if self.char_shape_spelling != 'OutLineType' and 'OutLineType' in items:
            items[self.char_shape_spelling] = items.pop('OutLineType')
        made = _LegacySet('CharShape', self.legacy_copies)
        made.items.update(items)
        self._char_shape_object = made
        return made

    @CharShape.setter
    def CharShape(self, value: Any) -> None:  # noqa: N802
        self.char_shape_puts.append(value)

    def _char_whole(self, props: dict[str, Any], items: dict[str, Any]) -> dict[str, Any]:
        """A whole CharShape set's items over a shape: what each item says, the rest as it was."""
        out = dict(props)
        per = {key: {lang: (props.get(key) or {}).get(lang, default) for lang in _LANGS}
               for key, default in (('Fonts', props['Font']), ('Ratios', props['Ratio']), ('Spacings', 0), ('RelSizes', 100), ('Offsets', 0))}
        prefixes = (('FaceName', 'Fonts'), ('Size', 'RelSizes'), ('Ratio', 'Ratios'), ('Spacing', 'Spacings'), ('Offset', 'Offsets'))
        for key, value in items.items():
            if key in ('Height', 'TextColor', 'ShadeColor', 'UnderlineColor'):
                out[key] = value
            elif key in ('Bold', 'Italic', 'UseKerning', 'UseFontSpace'):
                out[key] = bool(value)
            elif key == 'UnderlineType':
                out['Underline'] = _UNDERLINE_BACK[value]
            elif key == 'DiacSymMark':
                out['SymMark'] = value
            elif key.startswith('FontType'):
                assert value == 1, f'a font type this fake does not write: {key}'
            elif key in _UNRENDERED:
                assert value == _UNRENDERED[key], f'a CharShape item this fake does not write: {key}'
            elif key == 'BorderFill':
                pass
            else:
                prefix = next((pair for pair in prefixes if key.startswith(pair[0]) and key[len(pair[0]):] in _LANGS), None)
                if prefix is None:
                    raise AssertionError(f'unexpected CharShape item {key}')
                per[prefix[1]][key[len(prefix[0]):]] = value
        out.update(per)
        return canonical_char(out)

    def _char_with(self, props: dict[str, Any], items: dict[str, Any]) -> dict[str, Any]:
        """A sub-set's items over the shape it started from; a font per language kept per language."""
        if any(key in _WHOLE_ONLY for key in items):
            return self._char_whole(props, items)
        if 'Fonts' not in props:
            return super()._char_with(props, items)
        changed = super()._char_with(props, {key: value for key, value in items.items() if not key.startswith(('FaceName', 'FontType'))})
        fonts = dict(props['Fonts'])
        for lang in _LANGS:
            if f'FaceName{lang}' in items:
                fonts[lang] = items[f'FaceName{lang}']
        changed['Fonts'], changed['Font'] = fonts, fonts.get('Hangul', changed['Font'])
        return changed

    def _p(self, para: dict[str, Any], chars: list[dict[str, Any]], paras: list[dict[str, Any]], *, atoms: list[dict[str, Any]] | None = None) -> str:
        if any('run' in atom for atom in (para['atoms'] if atoms is None else atoms)):
            return self._p_with_runs(para, chars, paras, atoms=atoms)
        out = super()._p(para, chars, paras, atoms=atoms)
        head = out.index('>')
        return out[:head].replace('Style="0"', f'Style="{para.get("style", 0)}"', 1) + out[head:]

    def _p_with_runs(self, para: dict[str, Any], chars: list[dict[str, Any]], paras: list[dict[str, Any]], *, atoms: list[dict[str, Any]] | None = None) -> str:
        """The formatting fake's paragraph text, where a character's ``run`` holds TEXT attributes besides CharShape. No capture has such an
        attribute: these are synthetic, for what a run split, merge or retargeting does to them."""
        whole = atoms is None
        atoms = para['atoms'] if atoms is None else atoms
        inst = f' InstId="{para["p_inst"]}"' if para.get('p_inst') is not None else ''
        out = [f'<P ColumnBreak="false"{inst} PageBreak="{"true" if para.get("p_page_break") else "false"}" ParaShape="{paras.index(para["shape"])}" Style="{para.get("style", 0)}">']
        current, pending, raw_done = None, '', False

        def flush() -> None:
            nonlocal pending
            if pending:
                out.append(f'<CHAR>{pending}</CHAR>')
                pending = ''

        for atom in atoms:
            ref = chars.index(atom['cs'])
            run = tuple(sorted(atom.get('run', {}).items()))
            if (ref, run) != current:
                flush()
                if current is not None:
                    out.append('</TEXT>')
                out.append(f'<TEXT CharShape="{ref}"' + ''.join(f' {key}="{escape(value)}"' for key, value in run) + '>')
                current = (ref, run)
            if 'ctrl' not in atom:
                pending += _INLINE.get(atom['c'], escape(atom['c']))
                continue
            flush()
            if atom.get('raw'):
                if not raw_done:
                    out.append(_SECTION_HEAD)
                    raw_done = True
            elif 'cells' in atom:
                cells = iter(atom['cells'])
                out.append(re.sub(r'<P [^>]*>.*?</P>', lambda _m: self._p(next(cells), chars, paras), _TABLE, flags=re.S))
            elif 'drawtext' in atom:
                inner = ''.join(self._p(item, chars, paras) for item in atom['drawtext'])
                out.append(f'<RECTANGLE Ratio="0"><DRAWINGOBJECT><DRAWTEXT><PARALIST>{inner}</PARALIST></DRAWTEXT></DRAWINGOBJECT></RECTANGLE>')
            elif 'caption' in atom:
                inner = ''.join(self._p(item, chars, paras) for item in atom['caption'])
                out.append(_PICTURE.replace('</PICTURE>', f'<CAPTION><PARALIST>{inner}</PARALIST></CAPTION></PICTURE>'))
            else:
                out.append(atom['xml'])
        flush()
        out.append('</TEXT>')
        if whole and para.get('end') is not None and para['end'] != atoms[-1]['cs']:
            out.append(f'<TEXT CharShape="{chars.index(para["end"])}"/>')
        out.append('</P>')
        return ''.join(out)

    def _full_body(self) -> str:
        """An area atom may carry its own ``series`` (a wrong outcome: the area's SeriesNum renewed, nothing else)."""
        text = super()._full_body()
        tags = iter(self._area_atoms())

        def series(match: re.Match[str]) -> str:
            atom = next(tags)
            return match.group(0) if atom.get('series') is None else re.sub(r'SeriesNum="[0-9]+"', f'SeriesNum="{atom["series"]}"', match.group(0))

        return re.sub(r'<(?:HEADER|FOOTER) [^>]*>', series, text)

    def _style_xml(self, chars: list[dict[str, Any]], paras: list[dict[str, Any]]) -> str:
        rows = []
        for number, item in enumerate(self.styles):
            attrs = {'CharShape': str(chars.index(item['char']))}
            if item['eng'] is not None:
                attrs['EngName'] = item['eng']
            attrs.update(Id=str(number), LangId='1042', LockForm=str(item['lock']), Name=item['name'], NextStyle=str(item['next']))
            if item['para'] is not None:
                attrs['ParaShape'] = str(paras.index(item['para']))
            attrs['Type'] = item['type']
            rows.append('<STYLE ' + ' '.join(f'{key}="{escape(value, {chr(34): "&quot;"})}"' for key, value in attrs.items()) + '/>')
        return f'<STYLELIST Count="{len(rows)}">' + ''.join(rows) + '</STYLELIST>'

    def _document(self, body: str) -> str:
        chars, paras, _fonts = self._shapes()
        text = super()._document(body)
        text = re.sub(r'<STYLELIST [^>]*>.*?</STYLELIST>', lambda _m: self._style_xml(chars, paras), text, count=1, flags=re.S)
        if not self.font_info:
            return text

        def font(match: re.Match[str]) -> str:
            info = self.font_info.get(match.group(2).replace('&amp;', '&'))
            if info is None:
                return match.group(0)
            return match.group(1) + '><TYPEINFO ' + ' '.join(f'{key}="{value}"' for key, value in sorted(info.items())) + '/></FONT>'

        return re.sub(r'(<FONT Id="[0-9]+" Name="([^"]*)" Type="ttf")/>', font, text)

    # Where things are.
    def every_paragraph(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def visit(items: list[dict[str, Any]]) -> None:
            for para in items:
                found.append(para)
                for atom in para['atoms']:
                    for name in ('cells', 'drawtext', 'caption', 'paras'):
                        visit(atom.get(name, []))

        visit(self.body)
        return found

    def _caret_para(self) -> dict[str, Any]:
        return self.lists[self.cursor[0]][self.cursor[1]]

    def _caret_char(self) -> dict[str, Any]:
        para = self._caret_para()
        at, left = 0, None
        for atom in para['atoms']:
            if at >= self.cursor[2]:
                break
            left = atom['cs']
            at += width(atom)
        if left is not None:
            return dict(left)
        if para['atoms']:
            return dict(para['atoms'][0]['cs'])
        return dict(para.get('end') or char())

    def _plain(self, para: dict[str, Any], definition: dict[str, Any]) -> bool:
        return (para['shape'] == definition['para'] and all(atom['cs'] == definition['char'] for atom in para['atoms'])
                and (para.get('end') is None or para['end'] == definition['char']))

    def _renew(self, para: dict[str, Any]) -> None:
        para['p_inst'], self.next_inst = self.next_inst, self.next_inst + 1

    # The style actions.
    def style_defaults(self, name: str, hset: Any) -> Any:
        if self.defaults_result is not True:
            return self.defaults_result
        if getattr(hset, 'set_id', None) != 'Style':
            return False
        hset.reset()
        hset.items['Apply'] = self._caret_para().get('style', 0)
        return True

    def style_execute(self, name: str, hset: Any) -> Any:
        self.calls.append(('Execute', name, {key: value for key, value in hset.items.items() if key not in getattr(hset, 'subsets', ())},
                           self.selection))
        return self._executed(lambda: self._apply_style(hset.items['Apply']))

    def _executed(self, change: Any) -> Any:
        if self.execute_raises is not None:
            raise self.execute_raises
        if self.execute_result is not True:
            return self.execute_result
        before = self._snapshot()
        before['styles'] = copy.deepcopy(self.styles)
        change()
        self.undo_units.append({'state': before, 'selection': self.selection, 'cursor': self.cursor})
        self.redo_units = []
        self.IsModified = True
        if self.after_execute is not None:
            self.after_execute(self)
        return True

    def CreateAction(self, name: str) -> Any:  # noqa: N802
        if name in ('StyleAddNew', 'StyleDirectEdit'):
            return _LegacyStyleAction(self, name)
        return super().CreateAction(name)

    def legacy_defaults(self, name: str, pset: _LegacySet) -> Any:
        """The caret paragraph's style, and with legacy_subsets its shapes as the CharShape and ParaShape sub-sets."""
        current = self.styles[self._caret_para().get('style', 0)]
        pset.items.clear()
        pset.items.update(Type=0 if current['type'] == 'Para' else 1, NameLocal=current['name'], NameEng=current['eng'] or '',
                          Next=current['next'], LockForm=current['lock'])
        if self.legacy_subsets:
            # Written into the set itself, not through the copying API the product uses.
            caret_char, caret_para = self._caret_char(), dict(self._caret_para()['shape'])
            for item, items, base in (('CharShape', _char_items(caret_char), caret_char), ('ParaShape', _para_items(caret_para), caret_para)):
                if item == 'CharShape' and name == 'StyleDirectEdit' and not self.direct_edit_char_shape:
                    continue
                made = _LegacySet(item, pset.copies)
                if not self.legacy_subsets_unfilled:
                    made.items.update(items)
                    made.base = base
                pset.items[item] = made
        return self.legacy_defaults_result

    def legacy_execute(self, name: str, pset: _LegacySet) -> Any:
        sub = pset.items.get('CharShape')
        self.calls.append(('Execute', name, {key: value for key, value in pset.items.items() if not isinstance(value, _LegacySet)}, self.selection,
                           {key: (('set', value.set_id) if isinstance(value, _LegacySet) else value) for key, value in sub.items.items()}
                           if isinstance(sub, _LegacySet) else None))
        return self._executed(lambda: self._add_style(pset) if name == 'StyleAddNew' else self._change_style(pset))

    def _sub_char(self, pset: _LegacySet, base: dict[str, Any]) -> dict[str, Any]:
        sub = pset.items.get('CharShape')
        if not isinstance(sub, _LegacySet):
            return dict(base)
        # A sub-set GetDefault filled starts from the shape it read; one made with CreateItemSet from Hancom's factory shape.
        return self._char_with(sub.base if sub.base is not None else dict(self.factory_char), sub.items)

    def _touched(self) -> list[dict[str, Any]]:
        if self.selection is None:
            return [self._caret_para()]
        slist, spara, _spos, _elist, epara, epos = self.selection
        last = epara - 1 if epos == 0 and epara > spara else epara
        return self.lists[slist][spara:last + 1]

    def _apply_style(self, index: int) -> None:
        target = self.styles[index]
        if target['type'] == 'Char':
            if self.selection is None:
                raise AssertionError('a character style is only applied to a block here')
            for para, atoms in self._selected_atoms():
                for atom in atoms:
                    atom['cs'] = dict(target['char'])
                if atoms:
                    every = para['atoms']
                    low = next(number for number, atom in enumerate(every) if atom is atoms[0])
                    joins = lambda atom: atom.get('raw') or atom.get('joins')  # noqa: E731
                    for atom in every[:low] if all(joins(atom) for atom in every[:low]) else []:
                        atom['cs'] = dict(target['char'])
            return
        for para in self._touched():
            old = self.styles[para.get('style', 0)]
            para['style'] = index
            para['shape'] = dict(target['para'])
            for atom in para['atoms']:
                if self.apply_model == 'reset' or atom['cs'] == old['char']:
                    atom['cs'] = dict(target['char'])
            if para.get('end') is not None and (self.apply_model == 'reset' or para['end'] == old['char']):
                para['end'] = dict(target['char'])
            self._renew(para)

    def _add_style(self, pset: _LegacySet) -> None:
        items = pset.items
        caret = self._caret_para()
        kind = 'Para' if items.get('Type', 0) == 0 else 'Char'
        new = {'name': items['NameLocal'], 'eng': items.get('NameEng') or None, 'type': kind, 'char': self._sub_char(pset, self._caret_char()),
               'para': dict(caret['shape']) if kind == 'Para' else None, 'next': items.get('Next', 0), 'lock': items.get('LockForm', 0)}
        if self.add_renames_current:
            current = self.styles[caret.get('style', 0)]
            current['name'], current['eng'] = new['name'], new['eng']
            return
        at = caret.get('style', 0) + 1 if self.add_at is None else self.add_at
        old = list(self.styles)
        self.styles.insert(at, new)
        for para in self.every_paragraph():
            if para.get('style', 0) >= at:
                para['style'] = para.get('style', 0) + 1
        for item in old:
            if item['next'] >= at:
                item['next'] += 1
        if self.add_restyles_caret:
            caret['style'] = at

    def _change_style(self, pset: _LegacySet) -> None:
        items = pset.items
        named = [number for number, item in enumerate(self.styles) if item['name'] == items.get('NameLocal')]
        index = self.change_targets if self.change_targets is not None else (named[0] if named else None)
        if index is None:
            return
        old = copy.deepcopy(self.styles[index])
        changed = self.styles[index]
        changed['char'] = self._sub_char(pset, changed['char'])
        for item, key in (('NameEng', 'eng'), ('Next', 'next'), ('LockForm', 'lock')):
            if item in items:
                changed[key] = (items[item] or None) if key == 'eng' else items[item]
        targets = {index}
        if self.change_in_place_shared:
            for number, item in enumerate(self.styles):
                if number != index and item['char'] == old['char'] and item['para'] == old['para']:
                    item['char'], item['para'] = dict(changed['char']), (dict(changed['para']) if item['para'] is not None else None)
                    targets.add(number)
        if self.change_skips_uses:
            return
        for para in self.every_paragraph():
            if para.get('style', 0) in targets and self._plain(para, old):
                para['shape'] = dict(changed['para'])
                for atom in para['atoms']:
                    atom['cs'] = dict(changed['char'])
                if para.get('end') is not None:
                    para['end'] = dict(changed['char'])
                self._renew(para)

    # Inspection for tests.
    def style_names(self) -> list[str]:
        return [item['name'] for item in self.styles]

    def style_calls(self) -> list[tuple]:
        return [call for call in self.calls if call[0] in {'GetDefault', 'Execute'} and call[1] in STYLE_ACTIONS]


def styles_document(**options: Any) -> StyleHwp:
    """Paragraphs in each style and context the commands meet; the indexes are fixed for the tests."""
    body_style = default_styles()[1]
    bold = char(Bold=True)
    body = [
        paragraph('문서 제목'),                                                     # 0 바탕글, plain
        styled('본문 첫 문단', 1, para_shape=body_style['para']),                   # 1 본문, plain
        styled(('본문 둘째 ', ('굵은말', bold), ' 끝'), 1, para_shape=body_style['para']),  # 2 본문, a bold word
        paragraph('바탕 둘째 문단'),                                                 # 3 바탕글, plain
        paragraph(table(styled('셀 본문', 1, para_shape=body_style['para']), paragraph('셀 둘'), paragraph('셀 셋'), paragraph('셀 넷'))),  # 4, cells 5-8
        paragraph('범위 첫째'),                                                     # 9
        paragraph('범위 둘째'),                                                     # 10
        paragraph('번호 문단 꼬리', para_shape=shape(HeadingType='Number')),       # 11
        paragraph('마지막 문단'),                                                   # 12
    ]
    areas = [area('header', styled('머리 본문', 1, para_shape=body_style['para'])), area('footer', paragraph('꼬리 문구'))]
    return StyleHwp(body, areas=areas, **options)


__all__ = ['StyleHwp', 'area', 'char', 'default_styles', 'para_length', 'paragraph', 'shape', 'style', 'styled', 'styles_document']
