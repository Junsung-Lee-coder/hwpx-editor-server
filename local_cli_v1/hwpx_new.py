"""Generate a minimal blank ``.hwpx`` document locally.

The package layout and the reduced header follow the structure of the
``Skeleton.hwpx`` template shipped with python-hwpx (Apache-2.0), cut down to
the definitions a blank document references: two fonts per language, the
default border fills, one character shape, one paragraph shape, the outline
numbering and the ``바탕글`` style. Only paper size and margins are
parameters; orientation, columns and other layout changes are meant to be
applied afterwards with the native ``layout_exact`` edit, whose readback
proves the result.

A generated file is local evidence only: whether Hancom accepts it is proven
by opening it (``hwpx open``), not by this module.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

HWPUNIT_PER_MM = 7200 / 25.4  # 1 inch = 7200 HWPUNIT

PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    'A3': (297.0, 420.0),
    'A4': (210.0, 297.0),
    'A5': (148.0, 210.0),
    'B4': (257.0, 364.0),
    'B5': (182.0, 257.0),
    'LETTER': (215.9, 279.4),
    'LEGAL': (215.9, 355.6),
}

# Hancom's default A4 margins, in HWPUNIT (from the skeleton's section).
DEFAULT_MARGINS_HU = {'top': 5668, 'bottom': 4252, 'left': 8504, 'right': 8504, 'header': 4252, 'footer': 4252, 'gutter': 0}

_NS = {
    'ha': 'http://www.hancom.co.kr/hwpml/2011/app',
    'hp': 'http://www.hancom.co.kr/hwpml/2011/paragraph',
    'hp10': 'http://www.hancom.co.kr/hwpml/2016/paragraph',
    'hs': 'http://www.hancom.co.kr/hwpml/2011/section',
    'hc': 'http://www.hancom.co.kr/hwpml/2011/core',
    'hh': 'http://www.hancom.co.kr/hwpml/2011/head',
    'hhs': 'http://www.hancom.co.kr/hwpml/2011/history',
    'hm': 'http://www.hancom.co.kr/hwpml/2011/master-page',
    'hpf': 'http://www.hancom.co.kr/schema/2011/hpf',
    'dc': 'http://purl.org/dc/elements/1.1/',
    'opf': 'http://www.idpf.org/2007/opf/',
    'ooxmlchart': 'http://www.hancom.co.kr/hwpml/2016/ooxmlchart',
    'hwpunitchar': 'http://www.hancom.co.kr/hwpml/2016/HwpUnitChar',
    'epub': 'http://www.idpf.org/2007/ops',
    'config': 'urn:oasis:names:tc:opendocument:xmlns:config:1.0',
}
_XMLNS = ' '.join(f'xmlns:{prefix}="{uri}"' for prefix, uri in _NS.items())
_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
_LANGS = ('HANGUL', 'LATIN', 'HANJA', 'JAPANESE', 'OTHER', 'SYMBOL', 'USER')
_LANG_ATTRS = ('hangul', 'latin', 'hanja', 'japanese', 'other', 'symbol', 'user')
_FONTS = ('함초롬돋움', '함초롬바탕')
_TYPE_INFO = (
    '<hh:typeInfo familyType="FCAT_GOTHIC" weight="6" proportion="4" contrast="0" strokeVariation="1" '
    'armStyle="1" letterform="1" midline="1" xHeight="1"/>'
)
_OUTLINE_LEVELS = (
    ('DIGIT', '^1.', 0), ('HANGUL_SYLLABLE', '^2.', 0), ('DIGIT', '^3)', 0), ('HANGUL_SYLLABLE', '^4)', 0),
    ('DIGIT', '(^5)', 0), ('HANGUL_SYLLABLE', '(^6)', 0), ('CIRCLED_DIGIT', '^7', 1), ('CIRCLED_HANGUL_SYLLABLE', '^8', 1),
    ('HANGUL_JAMO', '', 0), ('ROMAN_SMALL', '', 1),
)


class HwpxNewError(ValueError):
    """The requested blank document cannot be generated."""


def mm_to_hwpunit(value_mm: float) -> int:
    return int(round(float(value_mm) * HWPUNIT_PER_MM))


def _lang_values(value: str) -> str:
    return ' '.join(f'{attr}="{value}"' for attr in _LANG_ATTRS)


def _border_fill(fill_id: int, *, brush: bool) -> str:
    sides = ''.join(
        f'<hh:{side} type="NONE" width="0.1 mm" color="#000000"/>'
        for side in ('leftBorder', 'rightBorder', 'topBorder', 'bottomBorder')
    )
    fill = '<hc:fillBrush><hc:winBrush faceColor="none" hatchColor="#999999" alpha="0"/></hc:fillBrush>' if brush else ''
    return (
        f'<hh:borderFill id="{fill_id}" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
        '<hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
        f'{sides}<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>{fill}</hh:borderFill>'
    )


def _paragraph_margin() -> str:
    parts = ''.join(f'<hc:{name} value="0" unit="HWPUNIT"/>' for name in ('intent', 'left', 'right', 'prev', 'next'))
    return f'<hh:margin>{parts}</hh:margin><hh:lineSpacing type="PERCENT" value="160" unit="HWPUNIT"/>'


def header_xml() -> str:
    fontfaces = ''.join(
        f'<hh:fontface lang="{lang}" fontCnt="{len(_FONTS)}">'
        + ''.join(f'<hh:font id="{index}" face="{face}" type="TTF" isEmbedded="0">{_TYPE_INFO}</hh:font>' for index, face in enumerate(_FONTS))
        + '</hh:fontface>'
        for lang in _LANGS
    )
    heads = ''.join(
        f'<hh:paraHead start="1" level="{level}" align="LEFT" useInstWidth="1" autoIndent="1" widthAdjust="0" '
        f'textOffsetType="PERCENT" textOffset="50" numFormat="{fmt}" charPrIDRef="4294967295" checkable="{checkable}"'
        + (f'>{escape(text)}</hh:paraHead>' if text else '/>')
        for level, (fmt, text, checkable) in enumerate(_OUTLINE_LEVELS, start=1)
    )
    char_pr = (
        '<hh:charPr id="0" height="1000" textColor="#000000" shadeColor="none" useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="2">'
        f'<hh:fontRef {_lang_values("1")}/><hh:ratio {_lang_values("100")}/><hh:spacing {_lang_values("0")}/>'
        f'<hh:relSz {_lang_values("100")}/><hh:offset {_lang_values("0")}/>'
        '<hh:underline type="NONE" shape="SOLID" color="#000000"/><hh:strikeout shape="NONE" color="#000000"/>'
        '<hh:outline type="NONE"/><hh:shadow type="NONE" color="#C0C0C0" offsetX="10" offsetY="10"/></hh:charPr>'
    )
    para_pr = (
        '<hh:paraPr id="0" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1" suppressLineNumbers="0" checked="0" textDir="LTR">'
        '<hh:align horizontal="JUSTIFY" vertical="BASELINE"/><hh:heading type="NONE" idRef="0" level="0"/>'
        '<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="BREAK_WORD" widowOrphan="0" keepWithNext="0" '
        'keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/><hh:autoSpacing eAsianEng="0" eAsianNum="0"/>'
        f'<hp:switch><hp:case hp:required-namespace="{_NS["hwpunitchar"]}">{_paragraph_margin()}</hp:case>'
        f'<hp:default>{_paragraph_margin()}</hp:default></hp:switch>'
        '<hh:border borderFillIDRef="2" offsetLeft="0" offsetRight="0" offsetTop="0" offsetBottom="0" connect="0" ignoreMargin="0"/></hh:paraPr>'
    )
    return (
        f'{_DECL}<hh:head {_XMLNS} version="1.5" secCnt="1">'
        '<hh:beginNum page="1" footnote="1" endnote="1" pic="1" tbl="1" equation="1"/><hh:refList>'
        f'<hh:fontfaces itemCnt="{len(_LANGS)}">{fontfaces}</hh:fontfaces>'
        f'<hh:borderFills itemCnt="2">{_border_fill(1, brush=False)}{_border_fill(2, brush=True)}</hh:borderFills>'
        f'<hh:charProperties itemCnt="1">{char_pr}</hh:charProperties>'
        '<hh:tabProperties itemCnt="1"><hh:tabPr id="0" autoTabLeft="0" autoTabRight="0"/></hh:tabProperties>'
        f'<hh:numberings itemCnt="1"><hh:numbering id="1" start="0">{heads}</hh:numbering></hh:numberings>'
        f'<hh:paraProperties itemCnt="1">{para_pr}</hh:paraProperties>'
        '<hh:styles itemCnt="1"><hh:style id="0" type="PARA" name="바탕글" engName="Normal" paraPrIDRef="0" '
        'charPrIDRef="0" nextStyleIDRef="0" langID="1042" lockForm="0"/></hh:styles></hh:refList>'
        '<hh:compatibleDocument targetProgram="HWP201X"><hh:layoutCompatibility/></hh:compatibleDocument>'
        '<hh:docOption><hh:linkinfo path="" pageInherit="0" footnoteInherit="0"/></hh:docOption>'
        '<hh:metaTag>{"name":""}</hh:metaTag><hh:trackchageConfig flags="56"/></hh:head>'
    )


def section_xml(page: dict[str, int]) -> str:
    note_line = '<hp:noteLine length="{length}" type="SOLID" width="0.12 mm" color="#000000"/>'
    note_format = '<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
    border_fills = ''.join(
        f'<hp:pageBorderFill type="{kind}" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">'
        '<hp:offset left="1417" right="1417" top="1417" bottom="1417"/></hp:pageBorderFill>'
        for kind in ('BOTH', 'EVEN', 'ODD')
    )
    text_width = page['width'] - page['left'] - page['right'] - page['gutter']
    return (
        f'{_DECL}<hs:sec {_XMLNS}><hp:p id="0" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
        '<hp:run charPrIDRef="0"><hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134" tabStop="8000" tabStopVal="4000" '
        'tabStopUnit="HWPUNIT" outlineShapeIDRef="1" memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0">'
        '<hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/><hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>'
        '<hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0" border="SHOW_ALL" fill="SHOW_ALL" '
        'hideFirstPageNum="0" hideFirstEmptyLine="0" showLineNumber="0"/>'
        '<hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/>'
        f'<hp:pagePr landscape="WIDELY" width="{page["width"]}" height="{page["height"]}" gutterType="LEFT_ONLY">'
        f'<hp:margin header="{page["header"]}" footer="{page["footer"]}" gutter="{page["gutter"]}" left="{page["left"]}" '
        f'right="{page["right"]}" top="{page["top"]}" bottom="{page["bottom"]}"/></hp:pagePr>'
        f'<hp:footNotePr>{note_format}{note_line.format(length=-1)}<hp:noteSpacing betweenNotes="283" belowLine="567" aboveLine="850"/>'
        '<hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="EACH_COLUMN" beneathText="0"/></hp:footNotePr>'
        f'<hp:endNotePr>{note_format}{note_line.format(length=14692344)}<hp:noteSpacing betweenNotes="0" belowLine="567" aboveLine="850"/>'
        '<hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="END_OF_DOCUMENT" beneathText="0"/></hp:endNotePr>'
        f'{border_fills}</hp:secPr><hp:ctrl><hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/></hp:ctrl>'
        '</hp:run><hp:run charPrIDRef="0"><hp:t/></hp:run><hp:linesegarray>'
        f'<hp:lineseg textpos="0" vertpos="0" vertsize="1000" textheight="1000" baseline="850" spacing="600" horzpos="0" '
        f'horzsize="{text_width}" flags="393216"/></hp:linesegarray></hp:p></hs:sec>'
    )


def content_hpf_xml(title: str, created: str) -> str:
    return (
        f'{_DECL}<opf:package {_XMLNS} version="" unique-identifier="" id=""><opf:metadata>'
        f'<opf:title>{escape(title)}</opf:title><opf:language>ko</opf:language>'
        '<opf:meta name="creator" content="text"/><opf:meta name="subject" content="text"/>'
        '<opf:meta name="description" content="text"/><opf:meta name="lastsaveby" content="text"/>'
        f'<opf:meta name="CreatedDate" content="text">{created}</opf:meta>'
        f'<opf:meta name="ModifiedDate" content="text">{created}</opf:meta><opf:meta name="keyword" content="text"/>'
        '</opf:metadata><opf:manifest><opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
        '<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
        '<opf:item id="settings" href="settings.xml" media-type="application/xml"/></opf:manifest>'
        '<opf:spine><opf:itemref idref="header" linear="yes"/><opf:itemref idref="section0" linear="yes"/></opf:spine></opf:package>'
    )


_VERSION_XML = (
    f'{_DECL}<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" tagetApplication="WORDPROCESSOR" '
    'major="5" minor="1" micro="1" buildNumber="0" os="1" xmlVersion="1.5" application="Hancom Office Hangul" '
    'appVersion="13, 0, 0, 1408 WIN32LEWindows_10"/>'
)
_SETTINGS_XML = (
    f'{_DECL}<ha:HWPApplicationSetting xmlns:ha="{_NS["ha"]}" xmlns:config="{_NS["config"]}">'
    '<ha:CaretPosition listIDRef="0" paraIDRef="0" pos="0"/></ha:HWPApplicationSetting>'
)
_CONTAINER_XML = (
    f'{_DECL}<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:hpf="{_NS["hpf"]}">'
    '<ocf:rootfiles><ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>'
    '<ocf:rootfile full-path="Preview/PrvText.txt" media-type="text/plain"/>'
    '<ocf:rootfile full-path="META-INF/container.rdf" media-type="application/rdf+xml"/></ocf:rootfiles></ocf:container>'
)
_MANIFEST_XML = f'{_DECL}<odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>'


def _container_rdf() -> str:
    pkg = 'http://www.hancom.co.kr/hwpml/2016/meta/pkg#'
    parts = ''.join(
        f'<rdf:Description rdf:about=""><ns0:hasPart xmlns:ns0="{pkg}" rdf:resource="{href}"/></rdf:Description>'
        f'<rdf:Description rdf:about="{href}"><rdf:type rdf:resource="{pkg}{kind}"/></rdf:Description>'
        for href, kind in (('Contents/header.xml', 'HeaderFile'), ('Contents/section0.xml', 'SectionFile'))
    )
    return (
        f'{_DECL}<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">{parts}'
        f'<rdf:Description rdf:about=""><rdf:type rdf:resource="{pkg}Document"/></rdf:Description></rdf:RDF>'
    )


def page_setup(*, paper: str = 'A4', width_mm: float | None = None, height_mm: float | None = None,
               margins_mm: dict[str, float] | None = None) -> dict[str, int]:
    """Resolve paper and margins to HWPUNIT, refusing layouts without a positive text area."""
    if width_mm is not None or height_mm is not None:
        if width_mm is None or height_mm is None:
            raise HwpxNewError('custom paper needs both width_mm and height_mm')
        size = (float(width_mm), float(height_mm))
    else:
        key = paper.strip().upper()
        if key not in PAPER_SIZES_MM:
            raise HwpxNewError(f'unknown paper {paper!r}; choose one of {", ".join(sorted(PAPER_SIZES_MM))} or give width/height')
        size = PAPER_SIZES_MM[key]
    if not all(50.0 <= value <= 1000.0 for value in size):
        raise HwpxNewError('paper width/height must be 50..1000 mm')
    page = {'width': mm_to_hwpunit(size[0]), 'height': mm_to_hwpunit(size[1]), **DEFAULT_MARGINS_HU}
    for name, value in (margins_mm or {}).items():
        if name not in DEFAULT_MARGINS_HU:
            raise HwpxNewError(f'unknown margin {name!r}')
        if value is None:
            continue
        if not 0.0 <= float(value) <= 300.0:
            raise HwpxNewError(f'margin {name} must be 0..300 mm')
        page[name] = mm_to_hwpunit(value)
    text_width = page['width'] - page['left'] - page['right'] - page['gutter']
    text_height = page['height'] - page['top'] - page['bottom'] - page['header'] - page['footer']
    if text_width < mm_to_hwpunit(10) or text_height < mm_to_hwpunit(10):
        raise HwpxNewError('margins leave less than 10 mm of text area')
    return page


def package_entries(page: dict[str, int], *, title: str = '', created: str | None = None) -> list[tuple[str, bytes]]:
    """Package members in Hancom's order; ``mimetype`` first and stored."""
    created = created or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return [
        ('mimetype', b'application/hwp+zip'),
        ('version.xml', _VERSION_XML.encode('utf-8')),
        ('Contents/header.xml', header_xml().encode('utf-8')),
        ('Contents/section0.xml', section_xml(page).encode('utf-8')),
        ('Preview/PrvText.txt', b'\r\n'),
        ('settings.xml', _SETTINGS_XML.encode('utf-8')),
        ('META-INF/container.rdf', _container_rdf().encode('utf-8')),
        ('Contents/content.hpf', content_hpf_xml(title, created).encode('utf-8')),
        ('META-INF/container.xml', _CONTAINER_XML.encode('utf-8')),
        ('META-INF/manifest.xml', _MANIFEST_XML.encode('utf-8')),
    ]


def write_blank_hwpx(path: str | Path, *, overwrite: bool = False, title: str = '', **page_options: Any) -> dict[str, Any]:
    """Write a blank document atomically; never replaces an existing file unless asked."""
    target = Path(path).expanduser()
    if target.suffix.lower() != '.hwpx':
        raise HwpxNewError('output file must end with .hwpx')
    if target.exists() and not overwrite:
        raise HwpxNewError(f'{target} already exists; pass --force to replace it')
    if len(title) > 200:
        raise HwpxNewError('title must be at most 200 characters')
    page = page_setup(**page_options)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix='.hwpx-new-', suffix='.hwpx', dir=target.parent)
    os.close(handle)
    try:
        with zipfile.ZipFile(temp_name, 'w') as archive:
            for name, data in package_entries(page, title=title):
                compress = zipfile.ZIP_STORED if name == 'mimetype' else zipfile.ZIP_DEFLATED
                archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data, compress_type=compress)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {
        'ok': True,
        'path': str(target),
        'evidence': 'generated-package',
        'page_hwpunit': page,
        'paper_mm': {'width': round(page['width'] / HWPUNIT_PER_MM, 1), 'height': round(page['height'] / HWPUNIT_PER_MM, 1)},
        'next': f'hwpx open {quoteattr(str(target))[1:-1]}  # Hancom must open it before it counts as a valid document',
    }
