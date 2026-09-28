"""Pure validation, planning and verification for exact layout edits.

Nothing here touches Hancom. The service mixin (``app.local_cli_layout``)
reads the editor through parameter-set readbacks (``HAction.GetDefault``)
and whole-document HWPML (``GetTextFile('HWPML2X', '')``); these helpers
decide whether an edit may run and whether the observed readback proves it
did exactly what was asked and nothing else.

Kinds:

* ``page_setup``: PageSetup action with the HSecDef parameter set
  (``PageDef`` sub-set), scoped with ``ApplyTo`` like pyhwpx ``set_pagedef``.
* ``columns``: MultiColumn action with the HColDef parameter set.
* ``section_insert``: BreakSection at the caret.
* ``section_delete``: DeleteBack at the very start of a section.
* ``hanging_indent``: ParagraphShapeIndentAtCaret just after a marker.

Units: Hancom HWPUNIT is 1/7200 inch, so 1 mm = 7200 / 25.4 = 283.4645669...
HWPUNIT. pyhwpx ``mili_to_hwp_unit`` delegates to the COM ``MiliToHwpUnit``,
which returns an integer; ``mm_to_hwpunit`` reproduces that by rounding to
the nearest integer.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any, Mapping

OP = 'layout_exact'
SCHEMA_VERSION = 'local-cli/layout-exact/v1'

KINDS = ('page_setup', 'columns', 'section_insert', 'section_delete', 'hanging_indent')

# 1 mm in HWPUNIT (1 HWPUNIT = 1/7200 inch; 1 inch = 25.4 mm).
HWPUNIT_PER_MM = 7200 / 25.4
MM_TOLERANCE = 0.1

# Public page_setup field -> HSecDef.PageDef item.
PAGE_FIELDS: dict[str, str] = {
    'paper_width_mm': 'PaperWidth',
    'paper_height_mm': 'PaperHeight',
    'landscape': 'Landscape',
    'margin_top_mm': 'TopMargin',
    'margin_bottom_mm': 'BottomMargin',
    'margin_left_mm': 'LeftMargin',
    'margin_right_mm': 'RightMargin',
    'header_len_mm': 'HeaderLen',
    'footer_len_mm': 'FooterLen',
    'gutter_len_mm': 'GutterLen',
}
# Every PageDef item (pyhwpx get_pagedef_as_dict). GutterType is never written.
PAGEDEF_ITEMS = ('PaperWidth', 'PaperHeight', 'Landscape', 'GutterType', 'TopMargin', 'BottomMargin',
                 'LeftMargin', 'RightMargin', 'HeaderLen', 'FooterLen', 'GutterLen')
PAPER_BOUNDS_MM = (10.0, 1000.0)
MARGIN_BOUNDS_MM = (0.0, 300.0)
# pyhwpx set_pagedef: ApplyTo 2 = current section ("cur"), 3 = whole document ("all").
PAGE_APPLY_TO = {'current_section': 2, 'whole_document': 3}

# HColDef items read back. Count/SameSize/SameGap are written; the rest must not move.
COLDEF_ITEMS = ('Count', 'SameSize', 'SameGap', 'Type', 'Layout', 'LineType', 'LineWidth', 'LineColor')
COLDEF_REQUIRED = ('Count', 'SameSize', 'SameGap')
MAX_COLUMNS = 10
GAP_BOUNDS_MM = (0.0, 100.0)
# ApplyTo codes for HColDef. UNVERIFIED natively: 2 mirrors HSecDef "current
# section"; 6 is the value recorded Hancom macros use for the multi-column
# dialog's "new columns from here". The HWPML COLDEF count check makes a wrong
# code fail verification instead of passing silently.
COLUMN_APPLY_TO = {'current_section': 2, 'from_caret_new': 6}

# HParaShape items compared for "nothing else changed" (pyhwpx apply_parashape list
# plus the heading/border scalars). Only scalar values are compared.
PARASHAPE_ITEMS = ('AlignType', 'BreakLatinWord', 'BreakNonLatinWord', 'LineSpacingType', 'LineSpacing', 'Condense',
                   'SnapToGrid', 'NextSpacing', 'PrevSpacing', 'Indentation', 'RightMargin', 'LeftMargin',
                   'PagebreakBefore', 'KeepLinesTogether', 'KeepWithNext', 'WidowOrphan', 'AutoSpaceEAsianNum',
                   'AutoSpaceEAsianEng', 'LineWrap', 'FontLineHeight', 'TextAlignment', 'HeadingType', 'Level')
MAX_MARKER_CHARS = 40

COMMON_KEYS = frozenset({'op', 'operation', 'label', 'kind', 'expected_pos', 'confirm_layout'})
KIND_KEYS: dict[str, frozenset[str]] = {
    'page_setup': frozenset({*PAGE_FIELDS, 'apply_to', 'expected_before'}),
    'columns': frozenset({'count', 'gap_mm', 'same_width', 'apply_to', 'expected_before'}),
    'section_insert': frozenset(),
    'section_delete': frozenset({'section_index'}),
    'hanging_indent': frozenset({'marker_text'}),
}
ALL_KEYS = sorted(COMMON_KEYS.union(*KIND_KEYS.values()))

# HWPML elements that define sections/columns; they move with section edits,
# so they are counted on their own and left out of the control inventory.
_LAYOUT_TAGS = frozenset({'SECDEF', 'COLDEF'})


class LayoutError(ValueError):
    """A request, plan or readback cannot be accepted."""


# ---------------------------------------------------------------- units


def mm_to_hwpunit(mm: float) -> int:
    return int(round(float(mm) * HWPUNIT_PER_MM))


def hwpunit_to_mm(value: float) -> float:
    return round(float(value) / HWPUNIT_PER_MM, 3)


def _tolerance_hwpunit() -> float:
    return MM_TOLERANCE * HWPUNIT_PER_MM


# ---------------------------------------------------------------- request validation


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_pos(value: Any, *, field: str = 'expected_pos') -> tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(_is_int(item) and item >= 0 for item in value):
        raise LayoutError(f'{field} must be [list, para, pos] with three non-negative integers')
    return int(value[0]), int(value[1]), int(value[2])


def _page_value(field: str, value: Any) -> Any:
    if field == 'landscape':
        if not isinstance(value, bool):
            raise LayoutError('landscape must be true or false')
        return value
    if not _is_number(value):
        raise LayoutError(f'{field} must be a finite number of millimetres')
    return float(value)


def _validate_page_setup(step: Mapping[str, Any]) -> dict[str, Any]:
    targets: dict[str, Any] = {}
    for field in PAGE_FIELDS:
        if step.get(field) is None:
            continue
        value = _page_value(field, step[field])
        if field.startswith('paper_'):
            low, high = PAPER_BOUNDS_MM
        elif field == 'landscape':
            targets[field] = value
            continue
        else:
            low, high = MARGIN_BOUNDS_MM
        if not (low <= value <= high):
            raise LayoutError(f'{field} must be {low:g}..{high:g} mm, got {value:g}')
        targets[field] = value
    if not targets:
        raise LayoutError(f'page_setup needs at least one of: {", ".join(PAGE_FIELDS)}')
    apply_to = step.get('apply_to') or 'current_section'
    if apply_to not in PAGE_APPLY_TO:
        raise LayoutError(f'page_setup apply_to must be one of: {", ".join(PAGE_APPLY_TO)}')
    expected = step.get('expected_before')
    if not isinstance(expected, Mapping):
        raise LayoutError('page_setup requires expected_before: the current value of every field being changed, from a read-only probe')
    unknown = sorted(key for key in expected if key not in PAGE_FIELDS)
    if unknown:
        raise LayoutError(f'expected_before has unknown fields: {", ".join(unknown)}')
    missing = sorted(key for key in targets if key not in expected)
    if missing:
        raise LayoutError(f'expected_before must include every field being changed; missing: {", ".join(missing)}')
    expected_before = {key: _page_value(key, value) for key, value in expected.items()}
    return {'targets': targets, 'apply_to': apply_to, 'expected_before': expected_before}


def _validate_columns(step: Mapping[str, Any]) -> dict[str, Any]:
    count = step.get('count')
    if not _is_int(count) or not (1 <= count <= MAX_COLUMNS):
        raise LayoutError(f'columns count must be an integer 1..{MAX_COLUMNS}')
    same_width = step.get('same_width', True)
    if same_width is None:
        same_width = True
    if not isinstance(same_width, bool):
        raise LayoutError('columns same_width must be true or false')
    gap_mm = step.get('gap_mm')
    if gap_mm is not None:
        if not _is_number(gap_mm) or not (GAP_BOUNDS_MM[0] <= float(gap_mm) <= GAP_BOUNDS_MM[1]):
            raise LayoutError(f'columns gap_mm must be {GAP_BOUNDS_MM[0]:g}..{GAP_BOUNDS_MM[1]:g} mm')
        if count < 2:
            raise LayoutError('columns gap_mm needs count >= 2')
        if not same_width:
            raise LayoutError('columns gap_mm only applies with same_width=true (per-column gaps are not supported)')
        gap_mm = float(gap_mm)
    apply_to = step.get('apply_to') or 'current_section'
    if apply_to not in COLUMN_APPLY_TO:
        raise LayoutError(f'columns apply_to must be one of: {", ".join(COLUMN_APPLY_TO)}')
    expected = step.get('expected_before')
    expected_before: dict[str, Any] | None = None
    if expected is not None:
        if not isinstance(expected, Mapping) or not expected:
            raise LayoutError('columns expected_before must be an object with count, gap_mm and/or same_width')
        expected_before = {}
        for key, value in expected.items():
            if key == 'count' and _is_int(value) and value >= 1:
                expected_before[key] = value
            elif key == 'gap_mm' and _is_number(value):
                expected_before[key] = float(value)
            elif key == 'same_width' and isinstance(value, bool):
                expected_before[key] = value
            else:
                raise LayoutError(f'columns expected_before.{key} is not a valid count/gap_mm/same_width value')
    return {'count': count, 'gap_mm': gap_mm, 'same_width': same_width, 'apply_to': apply_to, 'expected_before': expected_before}


def normalize_request(step: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a step and return the normalized request; raise LayoutError on anything off."""
    kind = step.get('kind')
    if kind not in KINDS:
        raise LayoutError(f'kind must be one of: {", ".join(KINDS)}')
    stray = sorted(key for key in ALL_KEYS if key not in COMMON_KEYS and key not in KIND_KEYS[kind] and step.get(key) is not None)
    if stray:
        raise LayoutError(f'kind {kind} does not take: {", ".join(stray)}')
    if step.get('confirm_layout') is not True:
        raise LayoutError('requires confirm_layout=true')
    request: dict[str, Any] = {'kind': kind, 'expected_pos': normalize_pos(step.get('expected_pos'))}
    if kind == 'page_setup':
        request.update(_validate_page_setup(step))
    elif kind == 'columns':
        request.update(_validate_columns(step))
    elif kind == 'section_delete':
        index = step.get('section_index')
        if not _is_int(index) or index < 2:
            raise LayoutError('section_delete requires section_index >= 2 (the section merged into its predecessor)')
        if request['expected_pos'][2] != 0:
            raise LayoutError('section_delete expected_pos must be at pos 0 (the very start of the section)')
        request['section_index'] = index
    elif kind == 'hanging_indent':
        marker = step.get('marker_text')
        if not isinstance(marker, str) or not (1 <= len(marker) <= MAX_MARKER_CHARS):
            raise LayoutError(f'hanging_indent marker_text must be 1..{MAX_MARKER_CHARS} characters')
        if any(char in marker for char in '\r\n\t') or not marker.strip():
            raise LayoutError('hanging_indent marker_text must be visible text without line breaks or tabs')
        request['marker_text'] = marker
    return request


def strict_normal(snapshot: Mapping[str, Any]) -> bool:
    """Normal edit state: no selection and selection mode int 0; anything unreadable is not normal."""
    mode = snapshot.get('selection_mode')
    return snapshot.get('has_selection') is False and _is_int(mode) and mode == 0


# ---------------------------------------------------------------- HWPML readback


def _canonical(element: ET.Element) -> str:
    attrs = ''.join(f' {key}={value!r}' for key, value in sorted(element.attrib.items()))
    text = (element.text or '').strip()
    children = ''.join(_canonical(child) for child in element)
    tail = (element.tail or '').strip()
    return f'<{element.tag}{attrs}>{text}{children}</{element.tag}>{tail}'


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def parse_layout_xml(xml_text: Any) -> dict[str, Any]:
    """Summarize whole-document HWPML for layout verification.

    * ``secdef_count``: SECDEF elements (one per section); ``section_elements``:
      BODY > SECTION elements. ``section_count`` is ``secdef_count`` when any
      SECDEF exists, else ``section_elements``.
    * ``pagedefs``: canonical PAGEDEF per SECDEF in order, or None when any
      SECDEF lacks exactly one PAGEDEF (per-section page checks then fail closed).
    * ``coldefs``: canonical COLDEF elements in document order.
    * ``text``: all CHAR text joined without separators, whitespace runs
      collapsed, so splitting or joining paragraphs does not change it.
    * ``controls``: tags of TEXT children other than CHAR/SECDEF/COLDEF.
    * ``para_shapes``: ParaShape attribute of every P in document order.
    """
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise LayoutError('HWPML readback returned no text')
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', xml_text, count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise LayoutError(f'HWPML readback is not well-formed: {exc}') from exc
    body = root if root.tag == 'BODY' else root.find('.//BODY')
    if body is None:
        raise LayoutError('HWPML readback has no BODY element')
    secdefs = list(body.iter('SECDEF'))
    section_elements = len(body.findall('SECTION'))
    pagedefs: list[str] | None = []
    for secdef in secdefs:
        found = list(secdef.iter('PAGEDEF'))
        if len(found) != 1:
            pagedefs = None
            break
        pagedefs.append(_canonical(found[0]))
    if not secdefs:
        pagedefs = None
    coldefs = [_canonical(element) for element in body.iter('COLDEF')]
    chars = ''.join(''.join(char.itertext()) for char in body.iter('CHAR'))
    controls = Counter(child.tag for text_el in body.iter('TEXT') for child in text_el if child.tag != 'CHAR' and child.tag not in _LAYOUT_TAGS)
    paragraphs = list(body.iter('P'))
    section_count = len(secdefs) if secdefs else section_elements
    if section_count < 1:
        raise LayoutError('HWPML readback has no SECDEF or SECTION element')
    return {
        'section_count': section_count,
        'secdef_count': len(secdefs),
        'section_elements': section_elements,
        'pagedefs': pagedefs,
        'coldefs': coldefs,
        'text': ' '.join(chars.split()),
        'controls': dict(sorted(controls.items())),
        'paragraph_count': len(paragraphs),
        'para_shapes': [p.get('ParaShape') for p in paragraphs],
    }


def public_document(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Document summary for responses: counts and short hashes, no text."""
    pagedefs = doc.get('pagedefs')
    return {
        'section_count': doc['section_count'],
        'secdef_count': doc['secdef_count'],
        'section_elements': doc['section_elements'],
        'coldef_count': len(doc['coldefs']),
        'paragraph_count': doc['paragraph_count'],
        'text_chars': len(doc['text']),
        'text_sha256': _digest(doc['text'])[:16],
        'pagedef_sha256': None if pagedefs is None else [_digest(item)[:16] for item in pagedefs],
        'controls': dict(doc['controls']),
    }


def compare_documents(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    section_delta: int = 0,
    coldef_delta: int = 0,
    pagedefs: str = 'same',
    coldefs_may_change: bool = False,
    paragraphs: str = 'same',
) -> list[str]:
    """Differences between two document summaries that the planned edit does not explain.

    ``pagedefs``: 'same' | 'free' (checked by the caller). ``paragraphs``:
    'same' (count and every ParaShape id unchanged) | 'one_shape' (count
    unchanged, at most one ParaShape id changed) | 'free'.
    """
    reasons: list[str] = []
    if after['text'] != before['text']:
        reasons.append(f'document text changed ({len(before["text"])} -> {len(after["text"])} chars after whitespace normalization)')
    if after['controls'] != before['controls']:
        reasons.append(f'embedded controls changed: {before["controls"]!r} -> {after["controls"]!r}')
    if after['secdef_count'] != before['secdef_count'] + section_delta:
        reasons.append(f'SECDEF count is {after["secdef_count"]}, expected {before["secdef_count"] + section_delta}')
    if before['secdef_count'] == 0 and after['section_elements'] != before['section_elements'] + section_delta:
        reasons.append(f'SECTION count is {after["section_elements"]}, expected {before["section_elements"] + section_delta}')
    if before['secdef_count'] and after['section_elements'] not in (before['section_elements'], before['section_elements'] + section_delta):
        reasons.append(f'SECTION element count moved from {before["section_elements"]} to {after["section_elements"]}')
    if len(after['coldefs']) != len(before['coldefs']) + coldef_delta:
        reasons.append(f'COLDEF count is {len(after["coldefs"])}, expected {len(before["coldefs"]) + coldef_delta}')
    elif not coldefs_may_change and coldef_delta == 0 and after['coldefs'] != before['coldefs']:
        reasons.append('a column definition (COLDEF) changed')
    if pagedefs == 'same' and after['pagedefs'] != before['pagedefs'] and section_delta == 0:
        reasons.append('a section page definition (PAGEDEF) changed')
    if paragraphs in ('same', 'one_shape'):
        if after['paragraph_count'] != before['paragraph_count']:
            reasons.append(f'paragraph count moved from {before["paragraph_count"]} to {after["paragraph_count"]}')
        else:
            changed = sum(1 for old, new in zip(before['para_shapes'], after['para_shapes']) if old != new)
            limit = 1 if paragraphs == 'one_shape' else 0
            if changed > limit:
                reasons.append(f'{changed} paragraphs changed paragraph shape, expected at most {limit}')
    return reasons


def _changed_indexes(before: list[str], after: list[str]) -> list[int]:
    return [index for index, (old, new) in enumerate(zip(before, after)) if old != new]


# ---------------------------------------------------------------- page_setup


def _as_int_items(values: Mapping[str, Any], items: tuple[str, ...], what: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        value = values.get(item)
        if not _is_number(value):
            raise LayoutError(f'{what} readback has no numeric {item} ({value!r})')
        out[item] = int(value)
    return out


def pagedef_mm(pagedef: Mapping[str, int]) -> dict[str, Any]:
    """PageDef in public units (mm; landscape as bool; gutter_type raw)."""
    out: dict[str, Any] = {}
    for field, item in PAGE_FIELDS.items():
        out[field] = bool(pagedef[item]) if field == 'landscape' else hwpunit_to_mm(pagedef[item])
    out['gutter_type'] = pagedef['GutterType']
    return out


def text_area_mm(pagedef: Mapping[str, int]) -> tuple[float, float]:
    """(width, height) of the body text area in mm.

    Landscape (1) swaps which paper side runs horizontally, as in pyhwpx
    ``set_table_width``. Header/footer lengths are subtracted like pyhwpx
    ``create_table``. The gutter reduces height for GutterType 2 (top), else width.
    """
    landscape = bool(pagedef['Landscape'])
    across = pagedef['PaperHeight'] if landscape else pagedef['PaperWidth']
    down = pagedef['PaperWidth'] if landscape else pagedef['PaperHeight']
    gutter = pagedef['GutterLen']
    top_gutter = pagedef['GutterType'] == 2
    width = across - pagedef['LeftMargin'] - pagedef['RightMargin'] - (0 if top_gutter else gutter)
    height = down - pagedef['TopMargin'] - pagedef['BottomMargin'] - pagedef['HeaderLen'] - pagedef['FooterLen'] - (gutter if top_gutter else 0)
    return hwpunit_to_mm(width), hwpunit_to_mm(height)


def _field_matches(field: str, want: Any, got_raw: int) -> bool:
    if field == 'landscape':
        return bool(got_raw) == want
    return abs(hwpunit_to_mm(got_raw) - want) <= MM_TOLERANCE + 1e-9


def plan_page_setup(request: Mapping[str, Any], before_raw: Mapping[str, Any], doc: Mapping[str, Any]) -> dict[str, Any]:
    before = _as_int_items(before_raw, PAGEDEF_ITEMS, 'PageSetup')
    stale = [
        f'{field} is {pagedef_mm(before)[field]!r}, expected_before says {want!r}'
        for field, want in request['expected_before'].items()
        if not _field_matches(field, want, before[PAGE_FIELDS[field]])
    ]
    if stale:
        raise LayoutError('page setup changed since it was probed (re-read it before editing): ' + '; '.join(stale))
    targets: dict[str, int] = {}
    for field, value in request['targets'].items():
        targets[PAGE_FIELDS[field]] = (1 if value else 0) if field == 'landscape' else mm_to_hwpunit(value)
    if all(_field_matches(field, value, before[PAGE_FIELDS[field]]) for field, value in request['targets'].items()):
        raise LayoutError('every requested page setup value already holds; nothing to change')
    merged = {**before, **targets}
    width, height = text_area_mm(merged)
    if width <= 0 or height <= 0:
        raise LayoutError(f'the text area would not stay positive ({width:g} x {height:g} mm)')
    if doc['section_count'] > 1:
        pagedefs = doc.get('pagedefs')
        if pagedefs is None or len(pagedefs) != doc['section_count']:
            raise LayoutError('the document has several sections but HWPML exposes no PAGEDEF per section, so other sections cannot be proven unchanged')
        if request['apply_to'] == 'whole_document' and len(set(pagedefs)) != 1:
            raise LayoutError('sections have different page setups; whole_document would overwrite their other fields, edit each section instead')
    return {
        'kind': 'page_setup',
        'apply_to': request['apply_to'],
        'apply_to_code': PAGE_APPLY_TO[request['apply_to']],
        'targets': targets,
        'targets_public': dict(request['targets']),
        'before': before,
        'before_mm': pagedef_mm(before),
        'text_area_mm': [width, height],
    }


def verify_page_setup(
    plan: Mapping[str, Any],
    after_raw: Mapping[str, Any],
    doc_before: Mapping[str, Any],
    doc_after: Mapping[str, Any],
    *,
    caret_section: int | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    try:
        after = _as_int_items(after_raw, PAGEDEF_ITEMS, 'PageSetup')
    except LayoutError as exc:
        return {'ok': False, 'reasons': [str(exc)], 'after_mm': None}
    for field, want in plan['targets_public'].items():
        if not _field_matches(field, want, after[PAGE_FIELDS[field]]):
            reasons.append(f'{field} reads back {pagedef_mm(after)[field]!r}, requested {want!r}')
    for item in PAGEDEF_ITEMS:
        if item not in plan['targets'] and after[item] != plan['before'][item]:
            reasons.append(f'{item} changed from {plan["before"][item]} to {after[item]} although it was not requested')
    reasons += compare_documents(doc_before, doc_after, pagedefs='free')
    before_defs, after_defs = doc_before.get('pagedefs'), doc_after.get('pagedefs')
    if doc_before['section_count'] > 1 or before_defs is not None:
        if before_defs is None or after_defs is None or len(before_defs) != len(after_defs):
            reasons.append('per-section PAGEDEF readback is unavailable after the edit')
        else:
            changed = _changed_indexes(before_defs, after_defs)
            if plan['apply_to'] == 'whole_document':
                if len(set(after_defs)) != 1 or len(changed) != len(after_defs):
                    reasons.append(f'whole_document did not give every section the same new page setup (changed sections {[i + 1 for i in changed]})')
            elif len(changed) != 1:
                reasons.append(f'current_section changed {len(changed)} sections ({[i + 1 for i in changed]}), expected exactly 1')
            elif len(after_defs) > 1 and (caret_section is None or changed[0] != caret_section - 1):
                reasons.append(f'the changed section is {changed[0] + 1}, but the caret is in section {caret_section!r}')
    return {'ok': not reasons, 'reasons': reasons[:10], 'after_mm': pagedef_mm(after)}


# ---------------------------------------------------------------- columns


def _read_optional_ints(values: Mapping[str, Any], items: tuple[str, ...]) -> dict[str, int | None]:
    return {item: int(values[item]) if _is_number(values.get(item)) else None for item in items}


def plan_columns(request: Mapping[str, Any], before_raw: Mapping[str, Any], doc: Mapping[str, Any]) -> dict[str, Any]:
    _as_int_items(before_raw, COLDEF_REQUIRED, 'MultiColumn')
    before = _read_optional_ints(before_raw, COLDEF_ITEMS)
    expected = request.get('expected_before') or {}
    stale: list[str] = []
    if 'count' in expected and before['Count'] != expected['count']:
        stale.append(f'count is {before["Count"]}, expected_before says {expected["count"]}')
    if 'same_width' in expected and bool(before['SameSize']) != expected['same_width']:
        stale.append(f'same_width is {bool(before["SameSize"])}, expected_before says {expected["same_width"]}')
    if 'gap_mm' in expected and abs(hwpunit_to_mm(before['SameGap']) - expected['gap_mm']) > MM_TOLERANCE + 1e-9:
        stale.append(f'gap_mm is {hwpunit_to_mm(before["SameGap"])}, expected_before says {expected["gap_mm"]}')
    if stale:
        raise LayoutError('column definition changed since it was probed: ' + '; '.join(stale))
    targets: dict[str, int] = {'Count': request['count'], 'SameSize': 1 if request['same_width'] else 0}
    if request.get('gap_mm') is not None:
        targets['SameGap'] = mm_to_hwpunit(request['gap_mm'])
    unchanged = targets['Count'] == before['Count'] and bool(targets['SameSize']) == bool(before['SameSize']) and targets.get('SameGap', before['SameGap']) == before['SameGap']
    if request['apply_to'] == 'current_section' and unchanged:
        raise LayoutError('the current column definition already matches; nothing to change')
    return {
        'kind': 'columns',
        'apply_to': request['apply_to'],
        'apply_to_code': COLUMN_APPLY_TO[request['apply_to']],
        'targets': targets,
        'before': before,
        'coldef_count_before': len(doc['coldefs']),
    }


def verify_columns(plan: Mapping[str, Any], after_raw: Mapping[str, Any], doc_before: Mapping[str, Any], doc_after: Mapping[str, Any]) -> dict[str, Any]:
    after = _read_optional_ints(after_raw, COLDEF_ITEMS)
    reasons: list[str] = []
    for item, want in plan['targets'].items():
        got = after[item]
        same = got is not None and (bool(got) == bool(want) if item == 'SameSize' else got == want)
        if not same:
            reasons.append(f'{item} reads back {got!r}, requested {want!r}')
    for item in COLDEF_ITEMS:
        if item in plan['targets']:
            continue
        if plan['before'][item] is not None and after[item] != plan['before'][item]:
            reasons.append(f'{item} changed from {plan["before"][item]} to {after[item]} although it was not requested')
    new_block = plan['apply_to'] == 'from_caret_new'
    reasons += compare_documents(doc_before, doc_after, coldef_delta=1 if new_block else 0, coldefs_may_change=not new_block)
    if new_block and len(doc_after['coldefs']) == len(doc_before['coldefs']) + 1:
        # Every pre-existing column definition must survive unchanged around the new one.
        remaining = list(doc_after['coldefs'])
        for old in doc_before['coldefs']:
            if old in remaining:
                remaining.remove(old)
        if len(remaining) != 1:
            reasons.append('an existing column definition (COLDEF) changed besides the new one')
    return {'ok': not reasons, 'reasons': reasons[:10], 'after': after}


# ---------------------------------------------------------------- sections


def plan_section_insert(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {'kind': 'section_insert', 'section_count_before': doc['section_count']}


def verify_section_insert(doc_before: Mapping[str, Any], doc_after: Mapping[str, Any]) -> dict[str, Any]:
    reasons = _section_coldef_reasons(doc_before, doc_after, 1)
    reasons += compare_documents(doc_before, doc_after, section_delta=1, coldef_delta=len(doc_after['coldefs']) - len(doc_before['coldefs']), pagedefs='free', paragraphs='free')
    before_defs, after_defs = doc_before.get('pagedefs'), doc_after.get('pagedefs')
    if before_defs is not None:
        if after_defs is None or len(after_defs) != len(before_defs) + 1:
            reasons.append('per-section PAGEDEF readback does not show exactly one new section')
        elif not any(after_defs[i] == after_defs[i - 1] and after_defs[:i] + after_defs[i + 1:] == before_defs for i in range(1, len(after_defs))):
            reasons.append('the new section does not inherit its neighbour\'s page setup, or another section changed')
    return {'ok': not reasons, 'reasons': reasons[:10]}


def _section_coldef_reasons(doc_before: Mapping[str, Any], doc_after: Mapping[str, Any], direction: int) -> list[str]:
    """A section break may carry its own COLDEF: allow exactly that one to come (insert) or go (delete).

    Every other column definition must survive unchanged (multiset containment).
    """
    old, new = list(doc_before['coldefs']), list(doc_after['coldefs'])
    delta = len(new) - len(old)
    if delta not in (0, direction):
        return [f'COLDEF count moved by {delta}, expected 0 or {direction}']
    smaller, larger = (old, list(new)) if direction > 0 else (new, list(old))
    for item in smaller:
        if item not in larger:
            return ['an existing column definition (COLDEF) changed']
        larger.remove(item)
    return []


def plan_section_delete(request: Mapping[str, Any], doc: Mapping[str, Any], *, key_indicator_sections: int | None) -> dict[str, Any]:
    index = request['section_index']
    if doc['section_count'] < 2:
        raise LayoutError('the document has a single section; there is no section break to delete')
    if index > doc['section_count']:
        raise LayoutError(f'section_index {index} is past the last section ({doc["section_count"]})')
    if key_indicator_sections is not None and key_indicator_sections != doc['section_count']:
        raise LayoutError(f'KeyIndicator reports {key_indicator_sections} sections but HWPML has {doc["section_count"]}')
    return {'kind': 'section_delete', 'section_index': index, 'section_count_before': doc['section_count']}


def verify_section_delete(plan: Mapping[str, Any], doc_before: Mapping[str, Any], doc_after: Mapping[str, Any]) -> dict[str, Any]:
    reasons = _section_coldef_reasons(doc_before, doc_after, -1)
    reasons += compare_documents(doc_before, doc_after, section_delta=-1, coldef_delta=len(doc_after['coldefs']) - len(doc_before['coldefs']), pagedefs='free', paragraphs='free')
    before_defs, after_defs = doc_before.get('pagedefs'), doc_after.get('pagedefs')
    if before_defs is not None:
        removed = plan['section_index'] - 1
        if after_defs != before_defs[:removed] + before_defs[removed + 1:]:
            reasons.append(f'surviving sections\' page setups are not exactly the old ones without section {plan["section_index"]}')
    return {'ok': not reasons, 'reasons': reasons[:10]}


# ---------------------------------------------------------------- hanging indent


def parashape_scalars(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if isinstance(value, (int, float, str, bool)) or value is None}


def plan_hanging_indent(request: Mapping[str, Any], paragraph_text: str, before_raw: Mapping[str, Any]) -> dict[str, Any]:
    marker = request['marker_text']
    if not isinstance(paragraph_text, str) or not paragraph_text.startswith(marker):
        preview = (paragraph_text or '')[: len(marker) + 10]
        raise LayoutError(f'the current paragraph does not start with marker_text {marker!r} (it starts with {preview!r})')
    shape = parashape_scalars(before_raw)
    _as_int_items(shape, ('Indentation', 'LeftMargin'), 'ParagraphShape')
    list_id, para, _pos = request['expected_pos']
    return {
        'kind': 'hanging_indent',
        'marker_text': marker,
        'marker_pos': (list_id, para, len(marker)),
        'before': shape,
    }


def verify_hanging_indent(
    plan: Mapping[str, Any],
    after_raw: Mapping[str, Any],
    text_before: str,
    text_after: str,
    doc_before: Mapping[str, Any],
    doc_after: Mapping[str, Any],
) -> dict[str, Any]:
    """Indentation must turn negative (hanging) and every other shape item stay put.

    Two LeftMargin models are accepted and reported, because which one
    ParagraphShapeIndentAtCaret uses is not natively verified: 'indent_only'
    (LeftMargin unchanged; Hancom's own 내어쓰기 representation) and
    'margin_shift' (LeftMargin grows by exactly the amount Indentation shrank).
    """
    before = plan['before']
    after = parashape_scalars(after_raw)
    reasons: list[str] = []
    model = None
    indent_before, left_before = before.get('Indentation'), before.get('LeftMargin')
    indent_after, left_after = after.get('Indentation'), after.get('LeftMargin')
    if not _is_number(indent_after) or not _is_number(left_after):
        reasons.append(f'ParagraphShape readback has no numeric Indentation/LeftMargin ({indent_after!r}, {left_after!r})')
    else:
        if indent_after >= 0:
            reasons.append(f'Indentation reads back {indent_after}, expected a negative (hanging) value')
        elif indent_after == indent_before:
            reasons.append('Indentation did not change')
        delta_indent = indent_after - indent_before
        delta_left = left_after - left_before
        if delta_left == 0:
            model = 'indent_only'
        elif delta_left == -delta_indent:
            model = 'margin_shift'
        else:
            reasons.append(f'LeftMargin moved by {delta_left}, expected 0 or {-delta_indent}')
    for key in sorted(set(before) | set(after)):
        if key in ('Indentation', 'LeftMargin'):
            continue
        if before.get(key) != after.get(key):
            reasons.append(f'paragraph shape {key} changed from {before.get(key)!r} to {after.get(key)!r}')
    if text_after != text_before:
        reasons.append('the paragraph text changed')
    reasons += compare_documents(doc_before, doc_after, paragraphs='one_shape')
    return {
        'ok': not reasons,
        'reasons': reasons[:10],
        'model': model,
        'indentation': [indent_before, indent_after],
        'left_margin': [left_before, left_after],
    }
