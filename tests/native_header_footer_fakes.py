"""A document with header and footer areas of their own, for the header and footer commands.

Built on tests.native_text_fakes and on what was measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2:
- every header and footer is a list of its own, and list ids follow document order: list 1 belongs to the section
  definition, then each header, footer and table cell takes the next id where it stands in the document;
- SetPos to a list id the document does not have returns True and leaves the caret where it was;
- ParentCtrl names 'head' or 'foot' while the caret is in a header or footer list, and nothing in the body;
- the control walk goes through the controls inside a header or footer, such as a page number, right after it;
- a section without a header or footer of its own shows the one of the section before it.
Source behavior only; the native runs are recorded separately.
"""
from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

from tests.native_format_fakes import _SECTION_HEAD, char, control, paragraph
from tests.native_numbering_fakes import FakeCtrl, chain
from tests.native_text_fakes import TextHwp

_SECTION_DEF = _SECTION_HEAD[:_SECTION_HEAD.index('</SECDEF>') + len('</SECDEF>')]
_COLUMN_DEF = re.search(r'<COLDEF [^>]*/>', _SECTION_HEAD).group(0)
_PLACEHOLDER = '<HFAREA/>'


def page_field(**fields: Any) -> dict[str, Any]:
    """A page number field as InsertPageNum leaves it in a header or footer."""
    return control('<AUTONUM Number="1" NumberType="Page"><AUTONUMFORMAT SideChar="" SuffixChar="" Superscript="false" Type="Digit" UserChar=""/></AUTONUM>',
                   'atno', **fields)


def area(kind: str, *paras: dict[str, Any], apply: str = 'Both', section: int = 0) -> dict[str, Any]:
    assert kind in {'header', 'footer'} and apply in {'Both', 'Odd', 'Even'}
    return {'ctrl': 'head' if kind == 'header' else 'foot', 'xml': _PLACEHOLDER, 'apply': apply, 'paras': list(paras), 'section': section}


class HeaderFooterHwp(TextHwp):
    def __init__(self, body: list[dict[str, Any]], *, areas: list[dict[str, Any]], sections: tuple[int, ...] = (), **options: Any) -> None:
        """sections: the body index where each section after the first starts."""
        self._building = True
        super().__init__(body, **options)
        first = self.body[0]['atoms']
        self.body[0]['atoms'] = [atom for atom in first if atom.get('ctrl') not in {'head', 'foot'}]
        for number, start in enumerate(sections, start=1):
            self.body[start]['section'] = number
            self.body[start]['atoms'][:0] = [{'cs': char(), **control(_SECTION_DEF, 'secd')}, {'cs': char(), **control(_COLUMN_DEF, 'cold')}]
        for item in areas:
            start = 0 if item['section'] == 0 else sections[item['section'] - 1]
            atoms = self.body[start]['atoms']
            lead = 0
            while lead < len(atoms) and atoms[lead].get('ctrl') in {'secd', 'cold', 'head', 'foot'}:
                lead += 1
            atoms.insert(lead, {'cs': char(), **{key: value for key, value in item.items() if key != 'section'}})
        self._building = False
        self._number_cells(self.body)

    # Lists in document order, and what holds each.
    def _number_cells(self, paragraphs: list[dict[str, Any]]) -> None:
        if getattr(self, '_building', False) or paragraphs is not self.body:
            return
        self.lists = {0: self.body, 1: [paragraph('')]}
        self.holders = {1: 'secd'}

        def visit(items: list[dict[str, Any]]) -> None:
            for para in items:
                for atom in para['atoms']:
                    if 'paras' in atom:
                        number = len(self.lists)
                        self.lists[number] = atom['paras']
                        self.holders[number] = atom['ctrl']
                        visit(atom['paras'])
                    for name in ('cells', 'drawtext', 'caption'):
                        for inner in atom.get(name, []):
                            number = len(self.lists)
                            self.lists[number] = [inner]
                            self.holders[number] = 'tbl' if name == 'cells' else 'gso'
                            visit([inner])

        visit(self.body)

    @property
    def ParentCtrl(self) -> FakeCtrl | None:  # noqa: N802
        holder = self.holders.get(self.cursor[0]) if self.cursor[0] else None
        return FakeCtrl(holder) if holder is not None else None

    @property
    def HeadCtrl(self) -> FakeCtrl | None:  # noqa: N802
        names: list[str] = []
        for para in self.body:
            for atom in para['atoms']:
                if 'ctrl' in atom:
                    names.append(atom['ctrl'])
                for inner in atom.get('paras', []):
                    names += [item['ctrl'] for item in inner['atoms'] if 'ctrl' in item]
        return chain([FakeCtrl(name) for name in names])

    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        if list_id not in self.lists:
            self.calls.append(('set_pos', list_id, para, pos))
            return True  # Measured: True, and the caret stays where it was.
        return super().set_pos(list_id, para, pos)

    def _shapes(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        chars, paras, fonts = super()._shapes()

        def visit(items: list[dict[str, Any]]) -> None:
            # An area's own paragraphs and those of the text boxes, tables and captions inside it.
            for para in items:
                if para['shape'] not in paras:
                    paras.append(para['shape'])
                for atom in para['atoms']:
                    if atom['cs'] not in chars:
                        chars.append(atom['cs'])
                    if atom['cs']['Font'] not in fonts:
                        fonts.append(atom['cs']['Font'])
                    for name in ('cells', 'drawtext', 'caption'):
                        visit(atom.get(name, []))
                if para.get('end') is not None and para['end'] not in chars:
                    chars.append(para['end'])

        for items in self._area_lists():
            visit(items)
        return chars, paras, fonts

    def _area_atoms(self) -> list[dict[str, Any]]:
        return [atom for para in self.body for atom in para['atoms'] if 'paras' in atom]

    def _area_lists(self) -> list[list[dict[str, Any]]]:
        return [atom['paras'] for atom in self._area_atoms()]

    def _full_body(self) -> str:
        chars, paras, _fonts = self._shapes()
        areas = iter(self._area_atoms())
        series = {'head': 0, 'foot': 0}

        def area_xml(_match: re.Match[str]) -> str:
            atom = next(areas)
            tag = 'HEADER' if atom['ctrl'] == 'head' else 'FOOTER'
            series[atom['ctrl']] += 1
            inner = ''.join(self._p(item, chars, paras) for item in atom['paras'])
            return (f'<{tag} ApplyPageType="{atom["apply"]}" SeriesNum="{series[atom["ctrl"]]}"><PARALIST HasNumRef="false" LineWrap="Break" '
                    f'TextDirection="0" VertAlign="Top">{inner}</PARALIST></{tag}>')

        parts = []
        for number, para in enumerate(self.body):
            text = self._p(para, chars, paras)
            if number == 0:
                text = re.sub(r'<HEADER .*?</HEADER>|<FOOTER .*?</FOOTER>', '', text, flags=re.S)
            if para.get('section'):
                parts.append(f'</SECTION><SECTION Id="{para["section"]}">')
            parts.append(re.sub(re.escape(_PLACEHOLDER), area_xml, text))
        return ''.join(parts)

    # Inspection for tests
    def area_text(self, number: int) -> list[str]:
        return [''.join(atom.get('c', '') for atom in para['atoms']) for para in self._area_lists()[number]]

    def area_props(self, number: int, para: int = 0) -> list[tuple[str, dict[str, Any]]]:
        return [(atom['c'], atom['cs']) for atom in self._area_lists()[number][para]['atoms'] if 'c' in atom]

    def area_controls(self, number: int, para: int = 0) -> list[str]:
        return [atom['ctrl'] for atom in self._area_lists()[number][para]['atoms'] if 'ctrl' in atom]


def report_document(**options: Any) -> HeaderFooterHwp:
    """One section: the header and footer of the owned demo document, with the page number field in the footer."""
    title = char(Height=1600, Bold=True)
    body = [
        paragraph(('Annual Plan 2025', title)),
        paragraph('This document sets out the annual plan for 2025. SAMPLE REPORT is its working name.'),
        paragraph('Last paragraph'),
    ]
    areas = [area('header', paragraph('SAMPLE REPORT')), area('footer', paragraph('Sample document ', page_field()))]
    return HeaderFooterHwp(body, areas=areas, **options)


def sections_document(**options: Any) -> HeaderFooterHwp:
    """Three sections: the first with a header and footer for every page, the second with odd and even headers and its own
    footer, and the third with none of its own, so it shows those of the second (measured)."""
    body = [
        paragraph('Section one body 2025'),  # 0
        paragraph('Section two first page body'),  # 1, section 1
        paragraph('Section two second page body'),  # 2
        paragraph('Section three body inherits 2025'),  # 3, section 2
    ]
    areas = [
        area('header', paragraph('SAMPLE REPORT')),
        area('footer', paragraph('Sample document ', page_field())),
        area('header', paragraph('ODD HEADER 2025'), apply='Odd', section=1),
        area('header', paragraph('EVEN HEADER 2025'), apply='Even', section=1),
        area('footer', paragraph('Section two footer ', page_field()), section=1),
    ]
    return HeaderFooterHwp(body, areas=areas, sections=(1, 3), **options)


__all__ = ['HeaderFooterHwp', 'area', 'page_field', 'report_document', 'sections_document', 'escape']
