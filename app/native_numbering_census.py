"""Protection census read from the live document's in-memory HWPML2X text.

Element and attribute names follow the HWPML 2.x shape as best understood and are NOT verified against
real Hancom output. The reader therefore matches names case-insensitively, never guesses, and reports
anything it cannot resolve as None (unproven) so callers refuse or report an unknown outcome instead of
treating a missing element as proof that nothing changed.

Census v2 keeps a full structural snapshot: every paragraph digest resolves its paragraph shape, character
shape, style, border fill, tab definition and media references to the referenced definition's content (the
definition's own Id is excluded), so a changed definition changes every digest that uses it. Definition
lists are kept as id-independent item multisets. Small allowed-delta policies live in native_numbering.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

MAX_HWPML_CHARS = 32 * 1024 * 1024
_MAX_DEPTH = 400
_HEADINGS = {'none': 'NONE', 'outline': 'OUTLINE', 'number': 'NUMBER', 'bullet': 'BULLET', '0': 'NONE', '1': 'OUTLINE', '2': 'NUMBER', '3': 'BULLET'}
_DOCTYPE = re.compile(r'<!\s*(DOCTYPE|ENTITY)', re.IGNORECASE)

PAGE_NUMBER_ELEMENTS = frozenset({'pagenum', 'pagenumctrl', 'pagenumpos'})
MANDATORY_DEFINITION_LISTS = ('charshapelist', 'parashapelist', 'stylelist')
_REFERENCE_LISTS = {
    'parashape': 'parashapelist',
    'charshape': 'charshapelist',
    'borderfill': 'borderfilllist',
    'tabdef': 'tabdeflist',
    'binitem': 'bindatalist',
}
_SHELL_LEVELS = frozenset({'1', '2', '3', '4'})
_MASKED_SHAPE_ATTRS = frozenset({'headingtype', 'level', 'headingidref'})
# Characters Hancom writes as an element inside CHAR, as observed on Hancom 2024. Anything else there stays a control.
_INLINE_CHARACTERS = {'tab': '\t', 'linebreak': '\n', 'nbspace': '\u00a0', 'fwspace': '\u2007'}


class CensusError(RuntimeError):
    """The HWPML2X text could not be read as a document census."""


class CensusTextUnusable(CensusError):
    """A plain native text string came back and cannot serve as evidence: too large, not XML, or not a document."""


class _Unresolved(Exception):
    """A reference or definition could not be resolved unambiguously."""


def _name(element: ET.Element) -> str:
    tag = element.tag if isinstance(element.tag, str) else ''
    return tag.rsplit('}', 1)[-1].lower()


def _attr(element: ET.Element, wanted: str) -> str | None:
    for key, value in element.attrib.items():
        if key.rsplit('}', 1)[-1].lower() == wanted:
            return value
    return None


def _lower_attrs(element: ET.Element) -> dict[str, str]:
    return {key.rsplit('}', 1)[-1].lower(): value for key, value in element.attrib.items()}


def _lower_attrs_all(element: ET.Element) -> list[tuple[str, str]]:
    return [(key.rsplit('}', 1)[-1].lower(), value) for key, value in element.attrib.items()]


def _is_page_restart(element: ET.Element) -> bool:
    return _name(element) == 'newnum' and (_lower_attrs(element).get('numbertype') or '').strip().lower() == 'page'


def _is_bare_page_restart(element: ET.Element) -> bool:
    """A leaf NEWNUM carrying exactly Number and NumberType=Page; anything else stays visible in every digest."""
    return (
        _is_page_restart(element)
        and len(element) == 0
        and not (element.text or '').strip()
        and len(element.attrib) == 2
        and set(_lower_attrs(element)) == {'number', 'numbertype'}
    )


def _vanishing_elements(root: ET.Element) -> set[int]:
    """Ids of elements the restart-stripped paragraph digest drops, so sibling positions ignore them like the digest does."""
    order: list[ET.Element] = []
    pending = [root]
    while pending:
        element = pending.pop()
        order.append(element)
        pending.extend(element)
    vanishing: set[int] = set()
    for element in reversed(order):
        name = _name(element)
        if name in PAGE_NUMBER_ELEMENTS or _is_bare_page_restart(element):
            vanishing.add(id(element))
        elif name != 'p' and len(element) and not (element.text or '').strip() and all(id(child) in vanishing for child in element):
            vanishing.add(id(element))
    return vanishing


def _section_page_starts(body: ET.Element) -> list[int | None]:
    """Each section definition's own page start in document order; 0 continues from 1, and None is a value this cannot read."""
    starts: list[int | None] = []
    for element in body.iter():
        if _name(element) == 'startnumber':
            value = (_lower_attrs(element).get('page') or '').strip()
            starts.append(int(value) if value.isascii() and value.isdigit() else None)
    return starts


def _page_restarts(
    root: ET.Element, body: ET.Element, paragraph_index: dict[int, int]
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """Document-order observations of every page-type NEWNUM in the whole document, inside or outside the body.

    The second result holds, for each observation, the count of surviving earlier siblings at every level of its path
    (the enclosing section down to the NEWNUM itself), so a restart moving beside its text siblings is visible.
    """
    found: list[dict[str, Any]] = []
    positions: list[list[int]] = []
    sections = [-1]
    vanishing = _vanishing_elements(root)

    def walk(
        element: ET.Element,
        ancestors: list[ET.Element],
        trail: list[int],
        in_body: bool,
        section: int | None,
        paragraph: ET.Element | None,
        depth: int,
    ) -> None:
        if depth > _MAX_DEPTH:
            raise CensusTextUnusable('HWPML2X nesting is too deep')
        survivors = 0
        for child in element:
            position = survivors
            if id(child) not in vanishing:
                survivors += 1
            name = _name(child)
            child_body = in_body or child is body
            child_section, child_paragraph = section, paragraph
            if in_body and name in {'header', 'footer'}:
                child_body = False
            if child_body and name == 'section':
                sections[0] += 1
                child_section = sections[0]
            if child_body and name == 'p':
                child_paragraph = child
            if _is_page_restart(child):
                if child_body:
                    start = next((i for i, item in enumerate(ancestors) if _name(item) == 'section'), None)
                    first = start if start is not None else ancestors.index(body)
                    chain = ancestors[first:]
                    owner = paragraph_index.get(id(paragraph)) if paragraph is not None else None
                    location, observed_section, observed_paragraph = 'body', section if start is not None else None, owner
                else:
                    first = 0
                    chain = ancestors
                    location, observed_section, observed_paragraph = 'outside', None, None
                positions.append([trail[index - 1] if index else 0 for index in range(first, len(ancestors))] + [position])
                found.append(
                    {
                        'location': location,
                        'section': observed_section,
                        'paragraph': observed_paragraph,
                        'path': [_name(item) for item in chain],
                        'attrs': sorted([key, value] for key, value in _lower_attrs(child).items()),
                        'children': len(child),
                        'text': child.text or '',
                        'tail': child.tail or '',
                    }
                )
            walk(child, ancestors + [child], trail + [position], child_body, child_section, child_paragraph, depth + 1)

    walk(root, [root], [], False, None, None, 0)
    return found, positions


def _digest_tokens(tokens: list[str]) -> str:
    return hashlib.sha256('\x1f'.join(tokens).encode('utf-8', 'surrogatepass')).hexdigest()


def _canonical(element: ET.Element, out: list[str], depth: int = 0, *, storage: bool = False) -> None:
    if depth > _MAX_DEPTH:
        raise CensusTextUnusable('HWPML2X nesting is too deep')
    name = _name(element)
    out.append('<' + name)
    for key, value in sorted((k.rsplit('}', 1)[-1].lower(), v) for k, v in element.attrib.items()):
        # Native reads report a different stored BINDATA Size for the same unchanged payload; only
        # that value is elided. Its presence, the payload and every other attribute stay bound.
        out.append(f'{key}=*' if storage and name == 'bindata' and key == 'size' else f'{key}={value}')
    out.append('>' + (element.text or ''))
    for child in element:
        _canonical(child, out, depth + 1, storage=storage)
        out.append('|' + (child.tail or ''))
    out.append('/')


def _element_digest(element: ET.Element, *, storage: bool = False) -> str:
    tokens: list[str] = []
    _canonical(element, tokens, storage=storage)
    return _digest_tokens(tokens)


def _para_text(paragraph: ET.Element, depth: int = 0) -> str:
    if depth > _MAX_DEPTH:
        raise CensusTextUnusable('HWPML2X nesting is too deep')
    parts: list[str] = []
    for child in paragraph:
        name = _name(child)
        if name in {'p', 'header', 'footer'}:
            continue
        if name == 'char':
            parts.append(child.text or '')
            for inner in child:
                parts.append(_INLINE_CHARACTERS.get(_name(inner), ''))
                parts.append(inner.tail or '')
        parts.append(_para_text(child, depth + 1))
    return ''.join(parts)


def _shape_table(root: ET.Element) -> dict[str, tuple[str | None, int | None]]:
    table: dict[str, tuple[str | None, int | None]] = {}
    for lst in root.iter():
        if _name(lst) != 'parashapelist':
            continue
        for shape in lst:
            if _name(shape) != 'parashape':
                continue
            shape_id = _attr(shape, 'id')
            if shape_id is None:
                continue
            raw_heading = _attr(shape, 'headingtype')
            heading = _HEADINGS.get(raw_heading.strip().lower()) if raw_heading is not None else None
            raw_level = _attr(shape, 'level')
            level: int | None = None
            if raw_level is not None and raw_level.strip().isdigit():
                level = int(raw_level.strip())
            elif raw_level is None and heading == 'NONE':
                level = 0
            if level is not None and level > 9:
                level = None
            table[shape_id.strip()] = (heading, level)
    return table


def _number_refs(root: ET.Element, attribute: str = 'headingidref') -> dict[str, str | None]:
    refs: dict[str, str | None] = {}
    for lst in root.iter():
        if _name(lst) != 'parashapelist':
            continue
        for shape in lst:
            shape_id = _attr(shape, 'id') if _name(shape) == 'parashape' else None
            if shape_id is not None:
                ref = _attr(shape, attribute)
                refs[shape_id.strip()] = ref.strip() if ref is not None else None
    return refs


def _outline_refs(body: ET.Element) -> set[str]:
    return {ref.strip() for element in body.iter() if _name(element) == 'secdef' and (ref := _attr(element, 'outlineshape')) is not None}


def _lists_digest(root: ET.Element, names: set[str]) -> str | None:
    found = [element for element in root.iter() if _name(element) in names]
    if not found:
        return None
    return _digest_tokens([_element_digest(element) for element in found])


def _numberings_digest(root: ET.Element, resolver: '_Resolver') -> str | None:
    """Definitions by content: a mark shape a definition names is renumbered when Hancom adds a character shape, and that is no change (observed on Hancom 2024)."""
    found = [element for element in root.iter() if _name(element) in {'numberinglist', 'bulletlist'}]
    if not found:
        return None
    try:
        return _digest_tokens([resolver.digest(element) for element in found])
    except _Unresolved:
        return _lists_digest(root, {'numberinglist', 'bulletlist'})


def _run_is_empty(run: ET.Element) -> bool:
    """No text and at most empty CHAR children; native Undo leaves one such run, with no element and nothing to show (observed on Hancom 2024)."""
    return not (run.text or '').strip() and all(
        _name(child) == 'char' and len(child) == 0 and not child.text and not (child.tail or '').strip() for child in run
    )


def _has_other_content(paragraph: ET.Element) -> bool:
    """An empty run is noise only beside something else; alone it is what holds an empty paragraph's character shape."""
    return any(_name(child) != 'text' or not _run_is_empty(child) for child in paragraph)


class _Resolver:
    """Digests elements with every definition reference replaced by the referenced definition's content."""

    def __init__(self, root: ET.Element, body: ET.Element) -> None:
        self.items: dict[str, list[ET.Element]] = {}
        self.index: dict[str, dict[str, ET.Element]] = {}
        self.ambiguous: set[tuple[str, str]] = set()
        self._memo: dict[tuple[str, int], str] = {}
        self._active: set[tuple[str, int]] = set()
        self._collect(root, body, 0)
        self.fonts = self._font_table()

    def _font_table(self) -> dict[tuple[str, str], str]:
        """Language-scoped font content by native Id; Hancom renumbers these Ids when it adds a font."""
        table: dict[tuple[str, str], str] = {}
        for face in self.items.get('facenamelist', []):
            lang = (_attr(face, 'lang') or '').strip().lower()
            for font in face:
                font_id = _attr(font, 'id') if _name(font) == 'font' else None
                if lang and font_id is not None:
                    table[(lang, font_id.strip())] = _digest_tokens(self.emit(font, root_item=True) or [])
        return table

    def font_items(self) -> list[str]:
        """One id-independent digest per language font, so an added font never reads as a changed one."""
        return [
            _digest_tokens([(_attr(face, 'lang') or '').strip().lower(), *(self.emit(font, root_item=True) or [])])
            for face in self.items.get('facenamelist', [])
            for font in face
            if _name(font) == 'font'
        ]

    def _collect(self, element: ET.Element, body: ET.Element, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise CensusTextUnusable('HWPML2X nesting is too deep')
        for child in element:
            if child is body:
                continue
            name = _name(child)
            if name.endswith('list') and name != 'hwpml':
                self.items.setdefault(name, []).extend(list(child))
                index = self.index.setdefault(name, {})
                for item in child:
                    native_binitem = name == 'bindatalist' and _name(item) == 'binitem'
                    item_id = _attr(item, 'id')
                    if native_binitem:
                        # Native BINDATALIST/BINITEM uses BinData; historical samples use Id.
                        # This is an identity alias only, never a general reference fallback.
                        identities = [(key, value.strip()) for key, value in _lower_attrs_all(item)
                                      if key in {'id', 'bindata'}]
                        if (not identities or any(not value for _key, value in identities)
                                or len({key for key, _value in identities}) != len(identities)
                                or len({value for _key, value in identities}) != 1):
                            raise CensusTextUnusable('BINITEM identity is missing or contradictory')
                        item_id = identities[0][1]
                    if item_id is None:
                        continue
                    key = item_id.strip()
                    if key in index:
                        if name == 'bindatalist' and (native_binitem or _name(index[key]) == 'binitem'):
                            raise CensusTextUnusable('BINITEM identity is duplicated')
                        self.ambiguous.add((name, key))
                    index[key] = item
            else:
                self._collect(child, body, depth + 1)

    def lookup(self, list_name: str, ref: str) -> ET.Element:
        key = ref.strip()
        element = self.index.get(list_name, {}).get(key)
        if not key or element is None or (list_name, key) in self.ambiguous:
            raise _Unresolved(f'{list_name} reference {ref!r} is missing or ambiguous')
        return element

    def definition(self, list_name: str, ref: str, *, masked: bool = False) -> str:
        return self.item_digest(self.lookup(list_name, ref), masked=masked)

    def item_digest(self, element: ET.Element, *, masked: bool = False, shell: bool = False) -> str:
        kind = ('shell' if shell else 'masked' if masked else 'full', id(element))
        cached = self._memo.get(kind)
        if cached is not None:
            return cached
        if kind in self._active:
            raise _Unresolved('definition references form a cycle')
        self._active.add(kind)
        try:
            tokens = self.emit(element, root_item=True, mask_shape=masked, shell=shell)
            assert tokens is not None
            digest = _digest_tokens(tokens)
        finally:
            self._active.discard(kind)
        self._memo[kind] = digest
        return digest

    def emit(
        self,
        element: ET.Element,
        *,
        strip: bool = False,
        strip_restart: bool = False,
        prune: bool = False,
        root_item: bool = False,
        mask_shape: bool = False,
        shell: bool = False,
        root_paragraph: bool = False,
        drop_empty_runs: bool = False,
        depth: int = 0,
    ) -> list[str] | None:
        if depth > _MAX_DEPTH:
            raise CensusTextUnusable('HWPML2X nesting is too deep')
        name = _name(element)
        is_root = depth == 0
        if strip and name in PAGE_NUMBER_ELEMENTS and not is_root:
            return None
        if strip_restart and not is_root and _is_bare_page_restart(element):
            return None
        if drop_empty_runs and not is_root and name == 'text' and _run_is_empty(element):
            return None
        if prune and name == 'p' and not is_root:
            return ['<p/>']
        if shell and name == 'parahead' and (_attr(element, 'level') or '').strip() in _SHELL_LEVELS:
            return None
        out = ['<' + name]
        heading = _HEADINGS.get((_attr(element, 'headingtype') or '').strip().lower())
        attributes = sorted((k.rsplit('}', 1)[-1].lower(), v) for k, v in element.attrib.items())
        single_inst_id = sum(1 for key, _value in attributes if key == 'instid') == 1
        for key, value in attributes:
            if key == 'outlineshape':
                continue
            if mask_shape and root_paragraph and depth == 0 and key == 'instid' and single_inst_id:
                continue
            if root_item and depth == 0 and key == 'id':
                continue
            if mask_shape and root_item and depth == 0 and key in _MASKED_SHAPE_ATTRS:
                continue
            if name == 'fontid' and (key, value.strip()) in self.fonts:
                out.append(f'{key}=@{self.fonts[(key, value.strip())]}')
            elif key in _REFERENCE_LISTS:
                masked = mask_shape and root_paragraph and depth == 0 and key == 'parashape'
                out.append(f'{key}=@{self.definition(_REFERENCE_LISTS[key], value, masked=masked)}')
            elif key == 'style' and name == 'p':
                out.append(f'{key}=@{self.definition("stylelist", value)}')
            elif key == 'headingidref' and heading in {'NUMBER', 'BULLET'}:
                target = 'numberinglist' if heading == 'NUMBER' else 'bulletlist'
                out.append(f'{key}=@{self.definition(target, value)}')
            else:
                out.append(f'{key}={value}')
        out.append('>' + (element.text or ''))
        children = list(element)
        kept = 0
        for child in children:
            sub = self.emit(
                child,
                strip=strip,
                strip_restart=strip_restart,
                prune=prune,
                shell=shell,
                drop_empty_runs=drop_empty_runs,
                depth=depth + 1,
            )
            if sub is None:
                if (child.tail or '').strip():
                    out.append('|' + (child.tail or ''))
                continue
            kept += 1
            out.extend(sub)
            out.append('|' + (child.tail or ''))
        out.append('/')
        if strip and children and kept == 0 and not is_root and not (element.text or '').strip():
            return None
        return out

    def digest(self, element: ET.Element, **options: Any) -> str:
        tokens = self.emit(element, **options)
        assert tokens is not None
        return _digest_tokens(tokens)


def _outline_bindings_complete(resolver: _Resolver, body: ET.Element) -> bool:
    sections = [element for element in body.iter() if _name(element) == 'section']
    if not sections:
        return False
    for section in sections:
        secdefs = [element for element in section.iter() if _name(element) == 'secdef']
        if not secdefs:
            return False
        for secdef in secdefs:
            ref = _attr(secdef, 'outlineshape')
            if ref is None:
                return False
            try:
                resolver.lookup('numberinglist', ref)
            except _Unresolved:
                return False
    return True


def live_protection_reader(hwp: Any) -> Callable[[], dict[str, Any]]:
    """Zero-argument reader of the live document's in-memory HWPML2X census; it never saves or rebinds the document."""

    def read() -> dict[str, Any]:
        getter = getattr(hwp, 'get_text_file', None)
        if not callable(getter):
            raise CensusError('get_text_file is unavailable on this machine')
        return parse_hwpml2x_census(getter('HWPML2X', ''))

    return read


def _definitions(resolver: _Resolver) -> dict[str, list[dict[str, str]]] | None:
    try:
        result: dict[str, list[dict[str, str]]] = {}
        for list_name, elements in resolver.items.items():
            if list_name == 'facenamelist':
                result[list_name] = sorted(
                    ({'sha': digest, 'shell': digest} for digest in resolver.font_items()), key=lambda entry: entry['sha'])
                continue
            entries = []
            for element in elements:
                sha = resolver.item_digest(element)
                shell = resolver.item_digest(element, shell=True) if list_name == 'numberinglist' else sha
                entries.append({'sha': sha, 'shell': shell})
            result[list_name] = sorted(entries, key=lambda entry: (entry['sha'], entry['shell']))
        return result
    except _Unresolved:
        return None


def _outline_shapes(resolver: _Resolver, body: ET.Element) -> list[str] | None:
    try:
        shapes: list[str] = []
        for element in body.iter():
            if _name(element) != 'secdef':
                continue
            ref = _attr(element, 'outlineshape')
            shapes.append('none' if ref is None else resolver.definition('numberinglist', ref))
        return shapes
    except _Unresolved:
        return None


def _styles_digest(root: ET.Element, resolver: _Resolver) -> str | None:
    """Styles by resolved definition content; a renumbered CharShape/ParaShape reference is not a style change."""
    if not any(_name(element) == 'stylelist' for element in root.iter()):
        return None
    try:
        return _digest_tokens([resolver.item_digest(element) for element in resolver.items.get('stylelist', [])])
    except _Unresolved:
        return _lists_digest(root, {'stylelist'})


def _paragraph_structure(element: ET.Element) -> dict[str, Any]:
    """Fixed facts a paragraph edit must know: native break flags and inline controls by element name."""
    controls = [_name(child) for run in element if _name(run) == 'text' for child in run if _name(child) != 'char']
    controls += [_name(inner) for run in element if _name(run) == 'text' for child in run if _name(child) == 'char'
                 for inner in child if _name(inner) not in _INLINE_CHARACTERS]
    return {
        'page_break': (_attr(element, 'pagebreak') or '').strip().lower() == 'true',
        'column_break': (_attr(element, 'columnbreak') or '').strip().lower() == 'true',
        'inline_controls': controls,
    }


def _paragraph_definitions(resolver: _Resolver, element: ET.Element, shape_ref: str | None, heading: str | None, number_ref: str | None) -> dict[str, Any]:
    """Which definitions this paragraph itself uses, so an edit can tell its own single-use definition from any other."""
    facts: dict[str, Any] = {'parashape_sha256': None, 'charshape_sha256s': [], 'numbering_ref_sha256': None, 'end_charshape_sha256': None}
    try:
        if shape_ref is not None:
            facts['parashape_sha256'] = resolver.definition('parashapelist', shape_ref)
        references = {ref for run in element if _name(run) == 'text' and (ref := _attr(run, 'charshape')) is not None}
        facts['charshape_sha256s'] = sorted(resolver.definition('charshapelist', ref) for ref in references)
        if heading in {'NUMBER', 'BULLET'} and number_ref is not None:
            facts['numbering_ref_sha256'] = resolver.definition('numberinglist' if heading == 'NUMBER' else 'bulletlist', number_ref)
        # A numbered paragraph's last run, even an empty one, is what its printed mark inherits (observed on Hancom 2024); the content digest drops empty runs.
        last = next((ref for run in reversed(list(element)) if _name(run) == 'text' and (ref := _attr(run, 'charshape')) is not None), None)
        if heading in {'NUMBER', 'BULLET', 'OUTLINE'} and last is not None:
            facts['end_charshape_sha256'] = resolver.definition('charshapelist', last)
    except _Unresolved:
        pass
    return facts


def parse_hwpml2x_census(text: Any) -> dict[str, Any]:
    if type(text) is not str:
        raise CensusError('HWPML2X text must be a string')
    if not text.strip():
        raise CensusTextUnusable('HWPML2X text is empty')
    if len(text) > MAX_HWPML_CHARS:
        raise CensusTextUnusable('HWPML2X text is too large to fingerprint')
    if _DOCTYPE.search(text):
        raise CensusTextUnusable('HWPML2X text declares a DOCTYPE or ENTITY, which is refused')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CensusTextUnusable(f'HWPML2X text is not well-formed XML: {exc}') from exc

    body = next((element for element in root.iter() if _name(element) == 'body'), None)
    if body is None:
        raise CensusTextUnusable('HWPML2X text has no BODY element')

    shapes = _shape_table(root)
    number_refs = _number_refs(root)
    # Native PARASHAPE names the definition in Heading (observed on Hancom 2024); the outline-sharing proof keeps its own input.
    native_heading_refs = _number_refs(root, 'heading')
    outline_refs = _outline_refs(body)
    resolver = _Resolver(root, body)
    outline_bindings_complete = _outline_bindings_complete(resolver, body)
    raw_paragraphs: list[tuple[int, str, str | None, ET.Element]] = []
    body_positions: dict[int, int] = {}
    header_footer_elements: list[ET.Element] = []
    section_counter = [-1]

    def visit(element: ET.Element, section: int, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise CensusTextUnusable('HWPML2X nesting is too deep')
        for child in element:
            name = _name(child)
            if name == 'section':
                section_counter[0] += 1
                visit(child, section_counter[0], depth + 1)
            elif name in {'header', 'footer'}:
                header_footer_elements.append(child)
            elif name == 'p':
                raw_paragraphs.append((max(section, 0), _para_text(child), _attr(child, 'parashape'), child))
                if _name(element) == 'section':
                    # Direct section paragraphs are the native list-0 paragraphs, in document order.
                    body_positions[id(child)] = len(body_positions)
                visit(child, section, depth + 1)
            else:
                visit(child, section, depth + 1)

    visit(body, 0, 0)

    paragraphs: list[dict[str, Any]] | None = []
    for section, text_value, shape_ref, element in raw_paragraphs:
        heading, level = shapes.get(shape_ref.strip(), (None, None)) if shape_ref is not None else (None, None)
        if heading is None or level is None:
            paragraphs = None
            break
        try:
            full = resolver.digest(element, prune=True, root_paragraph=True)
            masked = resolver.digest(element, prune=True, root_paragraph=True, mask_shape=True)
            stripped = resolver.digest(element, prune=True, root_paragraph=True, strip=True)
            restart_stripped = resolver.digest(element, prune=True, root_paragraph=True, strip=True, strip_restart=True)
            content = resolver.digest(element, prune=True, root_paragraph=True, mask_shape=True, strip=True, strip_restart=True, drop_empty_runs=_has_other_content(element))
        except _Unresolved:
            paragraphs = None
            break
        number_ref = number_refs.get(shape_ref.strip()) if shape_ref is not None else None
        native_ref = native_heading_refs.get(shape_ref.strip()) if shape_ref is not None else None
        definitions = _paragraph_definitions(resolver, element, shape_ref, heading, number_ref if number_ref is not None else native_ref)
        inst_ids = [value for key, value in _lower_attrs_all(element) if key == 'instid']
        shares_outline: bool | None
        if heading != 'NUMBER':
            shares_outline = False
        elif number_ref is None:
            shares_outline = None
        elif number_ref in outline_refs:
            shares_outline = True
        else:
            shares_outline = False if outline_bindings_complete else None
        paragraphs.append(
            {
                'shares_outline_numbering': shares_outline,
                'section': section,
                'text': text_value,
                'text_sha256': hashlib.sha256(text_value.encode('utf-8', 'surrogatepass')).hexdigest(),
                'heading': heading,
                'level': level,
                'inst_id': inst_ids[0] if len(inst_ids) == 1 else None,
                'inst_ids': sorted(inst_ids),
                'sha256': full,
                'masked_sha256': masked,
                'stripped_sha256': stripped,
                'restart_stripped_sha256': restart_stripped,
                'content_sha256': content,
                'body_index': body_positions.get(id(element)),
                **_paragraph_structure(element),
                **definitions,
            }
        )

    try:
        header_footer: list[str] | None = [resolver.digest(element) for element in header_footer_elements]
    except _Unresolved:
        header_footer = None
    try:
        frame: dict[str, str] | None = {
            'sha256': resolver.digest(body, prune=True),
            'stripped_sha256': resolver.digest(body, prune=True, strip=True),
        }
    except _Unresolved:
        frame = None

    page_restarts, page_restart_positions = _page_restarts(root, body, {id(item[3]): index for index, item in enumerate(raw_paragraphs)})
    return {
        'header_footer': header_footer,
        'body_sha256': _digest_tokens([text_value for _section, text_value, _shape, _element in raw_paragraphs]),
        'styles_sha256': _styles_digest(root, resolver),
        'numberings_sha256': _numberings_digest(root, resolver),
        'bindata_sha256': _lists_digest(root, {'bindatalist'}),
        # Explicitly fingerprint both media metadata and payload storage, including
        # observed absence. Unlike bindata_sha256=None, this is usable evidence
        # for the official-numbering text guard; it does not change page policy.
        'media_sha256': _digest_tokens([
            _element_digest(element, storage=_name(element) == 'bindatastorage') for element in root.iter()
            if _name(element) in {'bindatalist', 'bindatastorage'}
        ]),
        'page_number_count': None,
        'paragraphs': paragraphs,
        'definitions': _definitions(resolver),
        'frame': frame,
        'outline_shapes': _outline_shapes(resolver, body),
        'page_restarts': page_restarts,
        'page_restart_positions': page_restart_positions,
        'section_page_starts': _section_page_starts(body),
    }
