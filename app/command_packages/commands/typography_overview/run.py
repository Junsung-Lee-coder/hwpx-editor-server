from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from typing import Any, Mapping

from app.command_packages.commands.context.run import _as_dict, _safe_text, _warn

_SCOPE_VALUES = {'document'}
_TRUTHY = {'1', 'true', 'on', 'yes', 'bold'}
_FALSY = {'0', 'false', 'off', 'no', 'normal', ''}
_STYLE_ATTR_NAMES = (
    'CharShape',
    'CharShapeID',
    'CharShapeId',
    'CharShapeRef',
    'CharShapeRefID',
    'CharShapeRefId',
    'charShape',
    'charShapeID',
    'charShapeId',
    'charShapeRef',
    'charShapeRefID',
    'charShapeRefId',
    'charPrIDRef',
    'charPrID',
    'charPrRef',
    'CharPrIDRef',
    'CharPrID',
    'CharPrRef',
)
_CHAR_SHAPE_TAG_HINTS = {'charshape', 'charpr', 'charstyle', 'charshapetype'}
_SECTION_TAG_HINTS = {'section', 'sec', 'body', 'sectiondef'}
_PARAGRAPH_TAG_HINTS = {'p', 'para', 'paragraph'}


def validate_step(*, service: Any, index: int, step: dict[str, Any], manifest: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
    scope = str(step.get('scope') or 'document').strip().lower()
    if scope not in _SCOPE_VALUES:
        raise error_type(f'command-bundle step {index} typography_overview scope must be one of: {", ".join(sorted(_SCOPE_VALUES))}')
    step['scope'] = scope
    for key, default, cap in (('max_samples', 40, 200), ('max_sections', 30, 200), ('max_styles', 40, 200)):
        value = step.get(key)
        if value in (None, ''):
            step[key] = default
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise error_type(f'command-bundle step {index} typography_overview {key} must be a positive integer')
        step[key] = min(int(value), cap)
    return step


def _local_name(tag: Any) -> str:
    text = str(tag or '')
    if '}' in text:
        text = text.rsplit('}', 1)[-1]
    if ':' in text:
        text = text.rsplit(':', 1)[-1]
    return text


def _lower_name(tag: Any) -> str:
    return _local_name(tag).strip().lower()


def _attr(attrs: Mapping[str, Any], *names: str) -> Any:
    by_lower = {str(key).lower(): value for key, value in attrs.items()}
    for name in names:
        if name in attrs:
            return attrs[name]
        value = by_lower.get(name.lower())
        if value is not None:
            return value
    return None


def _first_attr_containing(attrs: Mapping[str, Any], *needles: str) -> Any:
    lowered = tuple(item.lower() for item in needles)
    for key, value in attrs.items():
        name = str(key).lower()
        if all(needle in name for needle in lowered) and value not in (None, ''):
            return value
    return None


def _clean_font_family(value: Any) -> str | None:
    if value in (None, ''):
        return None
    text = str(value).strip()
    if not text:
        return None
    # HWPML CHARSHAPE carries boolean attributes such as UseFontSpace="false".
    # Do not let those become a synthetic font family named "false".
    if text.lower() in {'true', 'false', 'on', 'off', 'yes', 'no'}:
        return None
    return text


def _face_from_attrs(attrs: Mapping[str, Any]) -> str | None:
    by_lower = {str(key).lower(): value for key, value in attrs.items()}
    exact_names = (
        'facenamehangul', 'facehangul', 'fontnamehangul', 'fonthangul',
        'facenamekorean', 'facekorean', 'fontnamekorean', 'fontkorean',
        'facename', 'fontname', 'face', 'name',
    )
    for name in exact_names:
        cleaned = _clean_font_family(by_lower.get(name))
        if cleaned:
            return cleaned
    for key, value in attrs.items():
        name = str(key).lower()
        if name.startswith('use') or name in {'usefontspace', 'usekerning', 'symmark'}:
            continue
        if 'facename' in name or 'fontname' in name or name.endswith('face'):
            cleaned = _clean_font_family(value)
            if cleaned:
                return cleaned
        if 'font' in name and any(lang in name for lang in ('hangul', 'korean', 'latin', 'hanja', 'japanese', 'other', 'symbol', 'user')):
            cleaned = _clean_font_family(value)
            if cleaned:
                return cleaned
    return None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or '').strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or '').strip()
    if not text:
        return None
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _font_size_from_raw(value: Any) -> float | None:
    raw = _to_float(value)
    if raw is None:
        return None
    # Hancom HWPML/COM char height is commonly 1/100 pt; keep small values as-is.
    if abs(raw) > 100:
        return round(raw / 100.0, 2)
    return round(raw, 2)


def _visible_text(value: str) -> str:
    return ' '.join(str(value or '').split())


def _char_count(value: str) -> int:
    return len(_visible_text(value))


def _hash_json(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8', errors='replace')
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _get_text_file(hwp: Any, format_name: str) -> str:
    for name in ('get_text_file', 'GetTextFile'):
        method = getattr(hwp, name, None)
        if not callable(method):
            continue
        value = method(format_name, '')
        if value is not None:
            return str(value)
    raise RuntimeError('Hancom GetTextFile/get_text_file is unavailable')


def _shape_id(attrs: Mapping[str, Any]) -> str | None:
    value = _attr(attrs, 'Id', 'ID', 'id', 'StyleId', 'StyleID', 'styleId', 'charShapeId', 'CharShapeId')
    if value in (None, ''):
        return None
    return str(value)


def _collect_font_faces(root: ET.Element) -> dict[tuple[str, str], str]:
    """Return {(language, font_id): face_name} from HWPML FONTFACE tables."""

    faces: dict[tuple[str, str], str] = {}
    for elem in root.iter():
        if 'fontface' not in _lower_name(elem.tag):
            continue
        lang = str(_attr(elem.attrib, 'Lang', 'lang', 'Language', 'language') or 'default').strip().lower() or 'default'
        for child in elem.iter():
            if child is elem:
                continue
            name = _lower_name(child.tag)
            if name not in {'font', 'fonttype'} and 'font' not in name:
                continue
            fid = _attr(child.attrib, 'Id', 'ID', 'id')
            face = _face_from_attrs(child.attrib)
            if fid in (None, '') or not face:
                continue
            key = str(fid)
            faces[(lang, key)] = face
            faces.setdefault(('*', key), face)
    return faces


def _face_from_font_id_attrs(attrs: Mapping[str, Any], font_faces: Mapping[tuple[str, str], str]) -> str | None:
    preferred = (
        ('hangul', ('Hangul', 'hangul', 'Korean', 'korean')),
        ('latin', ('Latin', 'latin')),
        ('hanja', ('Hanja', 'hanja')),
        ('japanese', ('Japanese', 'japanese')),
        ('other', ('Other', 'other')),
        ('symbol', ('Symbol', 'symbol')),
        ('user', ('User', 'user')),
    )
    for lang, names in preferred:
        fid = _attr(attrs, *names)
        if fid in (None, ''):
            continue
        key = str(fid)
        face = font_faces.get((lang, key)) or font_faces.get(('default', key)) or font_faces.get(('*', key))
        cleaned = _clean_font_family(face)
        if cleaned:
            return cleaned
    return None


def _style_ref(attrs: Mapping[str, Any]) -> str | None:
    value = _attr(attrs, *_STYLE_ATTR_NAMES)
    if value in (None, ''):
        # Some HWPML variants use generic style id names on run/text nodes.
        value = _first_attr_containing(attrs, 'char', 'id')
    if value in (None, ''):
        return None
    return str(value)


def _shape_from_element(elem: ET.Element, font_faces: Mapping[tuple[str, str], str] | None = None) -> dict[str, Any]:
    font_faces = font_faces or {}
    attrs = dict(elem.attrib)
    face = _face_from_attrs(attrs)
    height = (
        _attr(attrs, 'Height', 'height', 'TextHeight', 'textHeight', 'Size', 'size', 'FontSize', 'fontSize')
        or _first_attr_containing(attrs, 'height')
        or _first_attr_containing(attrs, 'size')
    )
    bold = _to_bool(_attr(attrs, 'Bold', 'bold', 'IsBold', 'isBold'))
    italic = _to_bool(_attr(attrs, 'Italic', 'italic', 'IsItalic', 'isItalic'))
    underline = _to_bool(_attr(attrs, 'Underline', 'underline', 'UnderLine', 'underLine', 'IsUnderline', 'isUnderline'))
    for child in elem.iter():
        if child is elem:
            continue
        child_attrs = dict(child.attrib)
        child_name = _lower_name(child.tag)
        if face in (None, ''):
            if 'fontid' in child_name:
                face = _face_from_font_id_attrs(child_attrs, font_faces)
            if face in (None, ''):
                face = _face_from_attrs(child_attrs)
        if height in (None, ''):
            height = _attr(child_attrs, 'Height', 'height', 'TextHeight', 'textHeight', 'Size', 'size', 'FontSize', 'fontSize')
        if bold is None:
            bold = _to_bool(_attr(child_attrs, 'Bold', 'bold', 'IsBold', 'isBold'))
        if italic is None:
            italic = _to_bool(_attr(child_attrs, 'Italic', 'italic', 'IsItalic', 'isItalic'))
        if underline is None:
            underline = _to_bool(_attr(child_attrs, 'Underline', 'underline', 'UnderLine', 'underLine', 'IsUnderline', 'isUnderline'))
    return {
        'font_family': _clean_font_family(face),
        'font_size_pt': _font_size_from_raw(height),
        'height_raw': height,
        'bold': bold,
        'italic': italic,
        'underline': underline,
    }


def _collect_char_shapes(root: ET.Element) -> dict[str, dict[str, Any]]:
    shapes: dict[str, dict[str, Any]] = {}
    font_faces = _collect_font_faces(root)
    for elem in root.iter():
        name = _lower_name(elem.tag)
        if not any(hint in name for hint in _CHAR_SHAPE_TAG_HINTS):
            continue
        sid = _shape_id(elem.attrib)
        if sid is None:
            continue
        shape = _shape_from_element(elem, font_faces)
        shape['id'] = sid
        shapes[sid] = shape
    return shapes


def _style_has_descendant(elem: ET.Element) -> bool:
    for child in elem:
        if _style_ref(child.attrib) is not None or _style_has_descendant(child):
            return True
    return False


def _style_variant_key(style: Mapping[str, Any], sid: str | None) -> tuple[str, str, str, str, str, str]:
    font = str(style.get('font_family') or (f'charshape:{sid}' if sid else 'unknown'))
    size = '' if style.get('font_size_pt') in (None, '') else str(style.get('font_size_pt'))
    bold = 'bold' if style.get('bold') is True else ('not-bold' if style.get('bold') is False else 'bold-unknown')
    italic = 'italic' if style.get('italic') is True else ('not-italic' if style.get('italic') is False else 'italic-unknown')
    underline = 'underline' if style.get('underline') is True else ('not-underline' if style.get('underline') is False else 'underline-unknown')
    return (font, size or 'unknown-size', bold, italic, underline, str(sid or 'no-charshape'))


def _counter_table(counter: Counter[str], total_chars: int, *, max_items: int | None = None) -> list[dict[str, Any]]:
    rows = []
    for key, chars in counter.most_common(max_items):
        rows.append({'value': key, 'chars': chars, 'ratio': round(chars / total_chars, 4) if total_chars else 0.0})
    return rows


def _variant_table(counter: Counter[tuple[str, str, str, str, str, str]], samples: Mapping[tuple[str, str, str, str, str, str], list[str]], total_chars: int, *, max_items: int) -> list[dict[str, Any]]:
    rows = []
    for key, chars in counter.most_common(max_items):
        font, size, bold, italic, underline, sid = key
        rows.append(
            {
                'font_family': font,
                'font_size_pt': None if size == 'unknown-size' else size,
                'bold': bold,
                'italic': italic,
                'underline': underline,
                'char_shape_id': sid,
                'chars': chars,
                'ratio': round(chars / total_chars, 4) if total_chars else 0.0,
                'samples': samples.get(key, [])[:3],
            }
        )
    return rows


def _font_size_distribution_table(
    font_counter: Counter[str],
    font_size_counters: Mapping[str, Counter[str]],
    font_bold_chars: Counter[str],
    font_italic_chars: Counter[str],
    font_underline_chars: Counter[str],
    total_chars: int,
    *,
    max_fonts: int,
    max_sizes: int = 8,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for font, chars in font_counter.most_common(max_fonts):
        size_counter = font_size_counters.get(font, Counter())
        size_rows = []
        for size, size_chars in size_counter.most_common(max_sizes):
            size_rows.append(
                {
                    'value': size,
                    'chars': size_chars,
                    'ratio': round(size_chars / chars, 4) if chars else 0.0,
                    'document_ratio': round(size_chars / total_chars, 4) if total_chars else 0.0,
                }
            )
        rows.append(
            {
                'font_family': font,
                'chars': chars,
                'ratio': round(chars / total_chars, 4) if total_chars else 0.0,
                'size_count': len(size_counter),
                'dominant_size': size_rows[0]['value'] if size_rows else None,
                'bold_char_ratio': round(font_bold_chars[font] / chars, 4) if chars else 0.0,
                'italic_char_ratio': round(font_italic_chars[font] / chars, 4) if chars else 0.0,
                'underline_char_ratio': round(font_underline_chars[font] / chars, 4) if chars else 0.0,
                'sizes': size_rows,
            }
        )
    return rows


def _parse_hwpml_typography(hwpml: str, *, max_samples: int, max_sections: int, max_styles: int, warnings: list[str]) -> dict[str, Any]:
    xml_text = re.sub(r'(<\?xml[^>]*?)\s+encoding=["\\\'][^"\\\']+["\\\']', r'\1', hwpml, count=1, flags=re.IGNORECASE)
    root = ET.fromstring(xml_text)
    char_shapes = _collect_char_shapes(root)
    font_counter: Counter[str] = Counter()
    size_counter: Counter[str] = Counter()
    font_size_counters: dict[str, Counter[str]] = defaultdict(Counter)
    font_bold_chars: Counter[str] = Counter()
    font_italic_chars: Counter[str] = Counter()
    font_underline_chars: Counter[str] = Counter()
    bold_chars = 0
    italic_chars = 0
    underline_chars = 0
    variant_counter: Counter[tuple[str, str, str, str, str, str]] = Counter()
    variant_samples: dict[tuple[str, str, str, str, str, str], list[str]] = defaultdict(list)
    section_counters: dict[int, dict[str, Any]] = defaultdict(lambda: {'chars': 0, 'font_counter': Counter(), 'size_counter': Counter(), 'variant_counter': Counter(), 'samples': []})
    unresolved_shape_ids: Counter[str] = Counter()
    total_chars = 0
    total_spans = 0
    section_index = 1
    paragraph_index = 0

    def add_span(style_id: str | None, text: str, section: int, paragraph: int) -> None:
        nonlocal total_chars, total_spans, bold_chars, italic_chars, underline_chars
        chars = _char_count(text)
        if chars <= 0:
            return
        style = char_shapes.get(str(style_id)) if style_id is not None else None
        if style is None:
            if style_id is not None:
                unresolved_shape_ids[str(style_id)] += chars
            style = {'font_family': None, 'font_size_pt': None, 'bold': None, 'italic': None, 'underline': None}
        font = str(style.get('font_family') or (f'charshape:{style_id}' if style_id is not None else 'unknown'))
        size_value = style.get('font_size_pt')
        size = str(size_value) if size_value not in (None, '') else 'unknown'
        key = _style_variant_key(style, style_id)
        total_chars += chars
        total_spans += 1
        font_counter[font] += chars
        size_counter[size] += chars
        font_size_counters[font][size] += chars
        if style.get('bold') is True:
            bold_chars += chars
            font_bold_chars[font] += chars
        if style.get('italic') is True:
            italic_chars += chars
            font_italic_chars[font] += chars
        if style.get('underline') is True:
            underline_chars += chars
            font_underline_chars[font] += chars
        variant_counter[key] += chars
        sample = _safe_text(_visible_text(text), max_chars=120)
        if sample and len(variant_samples[key]) < 3 and sum(len(v) for v in variant_samples.values()) < max_samples * 3:
            variant_samples[key].append(sample)
        sec = section_counters[int(section)]
        sec['chars'] += chars
        sec['font_counter'][font] += chars
        sec['size_counter'][size] += chars
        sec['variant_counter'][key] += chars
        if sample and len(sec['samples']) < 3:
            sec['samples'].append({'paragraph_index': paragraph, 'text': sample, 'style': {'font_family': font, 'font_size_pt': size, 'char_shape_id': style_id}})

    def walk(elem: ET.Element, inherited_style: str | None, current_section: int, current_paragraph: int) -> None:
        nonlocal section_index, paragraph_index
        name = _lower_name(elem.tag)
        section = current_section
        paragraph = current_paragraph
        if name in _SECTION_TAG_HINTS or name.endswith('section'):
            if elem is not root:
                section_index += 1
                section = section_index
        if name in _PARAGRAPH_TAG_HINTS:
            paragraph_index += 1
            paragraph = paragraph_index
        style_id = _style_ref(elem.attrib) or inherited_style
        if style_id is not None and not _style_has_descendant(elem):
            text = ''.join(elem.itertext())
            add_span(style_id, text, section, paragraph)
            return
        for child in list(elem):
            walk(child, style_id, section, paragraph)
        if elem.text and style_id is not None:
            add_span(style_id, elem.text, section, paragraph)

    walk(root, None, section_index, paragraph_index)
    if total_chars == 0:
        _warn(warnings, 'HWPML style-run text extraction returned zero visible characters; schema may use unsupported run/style tags')
    if unresolved_shape_ids:
        _warn(warnings, f'unresolved char shape ids present: {len(unresolved_shape_ids)}')
    if not font_counter:
        _warn(warnings, 'font family counts unavailable from HWPML char-shape mapping')
    if not size_counter:
        _warn(warnings, 'font size counts unavailable from HWPML char-shape mapping')

    sections = []
    for section, data in sorted(section_counters.items())[:max_sections]:
        chars = int(data['chars'])
        variants = data['variant_counter']
        anomalies = []
        if len(data['font_counter']) > 3:
            anomalies.append('many_font_families')
        if len(data['size_counter']) > 5:
            anomalies.append('many_font_sizes')
        if len(variants) > 8:
            anomalies.append('many_style_variants')
        sections.append(
            {
                'section_index': section,
                'chars': chars,
                'font_count': len(data['font_counter']),
                'size_count': len(data['size_counter']),
                'style_variant_count': len(variants),
                'top_fonts': _counter_table(data['font_counter'], chars, max_items=5),
                'top_sizes': _counter_table(data['size_counter'], chars, max_items=5),
                'samples': data['samples'][:3],
                'anomalies': anomalies,
            }
        )

    global_anomalies = []
    if len(font_counter) > 4:
        global_anomalies.append('many_font_families')
    if len(size_counter) > 8:
        global_anomalies.append('many_font_sizes')
    if variant_counter and bold_chars == 0:
        global_anomalies.append('bold_not_detected')
    if unresolved_shape_ids:
        global_anomalies.append('unresolved_char_shape_ids')

    return {
        'backend': 'hancom_gettextfile_hwpml2x',
        'hwpml_sha256': 'sha256:' + hashlib.sha256(hwpml.encode('utf-8', errors='replace')).hexdigest(),
        'hwpml_chars': len(hwpml),
        'char_shape_definition_count': len(char_shapes),
        'summary': {
            'total_spans': total_spans,
            'total_chars': total_chars,
            'unique_font_families': len(font_counter),
            'unique_font_sizes': len(size_counter),
            'unique_style_variants': len(variant_counter),
            'bold_char_ratio': round(bold_chars / total_chars, 4) if total_chars else 0.0,
            'italic_char_ratio': round(italic_chars / total_chars, 4) if total_chars else 0.0,
            'underline_char_ratio': round(underline_chars / total_chars, 4) if total_chars else 0.0,
            'global_anomalies': global_anomalies,
        },
        'global_fonts': _counter_table(font_counter, total_chars, max_items=max_styles),
        'global_sizes': _counter_table(size_counter, total_chars, max_items=max_styles),
        'font_size_distribution': _font_size_distribution_table(
            font_counter,
            font_size_counters,
            font_bold_chars,
            font_italic_chars,
            font_underline_chars,
            total_chars,
            max_fonts=max_styles,
        ),
        'style_variants': _variant_table(variant_counter, variant_samples, total_chars, max_items=max_styles),
        'sections': sections,
        'pages': [],
        'page_scope_available': False,
        'page_scope_note': 'HWPML2X does not reliably expose rendered page membership; use Hancom PDF/contact-sheet proof for page-level visual placement.',
        'unresolved_char_shape_ids': _counter_table(unresolved_shape_ids, sum(unresolved_shape_ids.values()), max_items=20),
        'evidence_limitations': [
            'Read-only live Hancom GetTextFile(HWPML2X) evidence; not raw HWPX/ZIP mutation.',
            'Counts are character-weighted by visible extracted HWPML text runs, not rendered glyph pixels.',
            'Rendered page membership is unavailable in this first native overview; combine with contact sheets/page crops.',
            'Table-vs-body distinction depends on HWPML run context and is not guaranteed in this first version.',
        ],
    }


def _write_artifact(handle: Any, report: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    artifact_dir = handle.session_root / 'typography_overview'
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f'typography-overview-{int(time.time() * 1000)}.json'
    data = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    artifact_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > 250_000:
        _warn(warnings, 'typography overview artifact is large; CLI output should remain compact and reference this artifact path')
    return {'path': str(artifact_path), 'sha256': f'sha256:{digest}', 'bytes': len(data)}


def run_step(*, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None, manifest: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    warnings: list[str] = []
    scope = str(step.get('scope') or 'document').strip().lower()
    max_samples = int(step.get('max_samples') or 40)
    max_sections = int(step.get('max_sections') or 30)
    max_styles = int(step.get('max_styles') or 40)
    try:
        hwpml = _get_text_file(handle.hwp, 'HWPML2X')
    except Exception as exc:  # pragma: no cover - live Hancom runtime-specific.
        raise RuntimeError(f'typography_overview GetTextFile(HWPML2X) failed: {type(exc).__name__}: {exc}') from exc
    try:
        overview = _parse_hwpml_typography(
            hwpml,
            max_samples=max_samples,
            max_sections=max_sections,
            max_styles=max_styles,
            warnings=warnings,
        )
    except ET.ParseError as exc:
        raise RuntimeError(f'typography_overview HWPML parse failed: {exc}') from exc
    document = {}
    try:
        page_evidence = service._bundle_page_evidence(handle.hwp)  # noqa: SLF001 - read-only native page evidence helper.
        document['page_evidence'] = page_evidence
        if page_evidence.get('page') not in (None, ''):
            document['current_page'] = page_evidence.get('page')
    except Exception:
        pass
    # Keep document metadata compact and non-authoritative; active session status remains the source of truth.
    report = {
        'schema_version': manifest.get('version') or 'local-cli/typography-overview/v1-package',
        'ok': True,
        'read_only': True,
        'scope': scope,
        'summary': f"typography overview: chars={overview['summary']['total_chars']} fonts={overview['summary']['unique_font_families']} sizes={overview['summary']['unique_font_sizes']} variants={overview['summary']['unique_style_variants']}",
        'document': document,
        'generated': {
            'epoch_ms': int(time.time() * 1000),
            'backend': overview['backend'],
        },
        **overview,
        'caps': {
            'max_samples': max_samples,
            'max_sections': max_sections,
            'max_styles': max_styles,
            'truncated': len(overview.get('sections') or []) >= max_sections or len(overview.get('style_variants') or []) >= max_styles,
        },
        'warnings': warnings,
    }
    artifact = _write_artifact(handle, report, warnings)
    report['artifact'] = artifact
    report['caps']['raw_artifact_path'] = artifact['path']
    report['caps']['raw_artifact_sha256'] = artifact['sha256']
    report['caps']['raw_artifact_bytes'] = artifact['bytes']
    return report, False, warnings
