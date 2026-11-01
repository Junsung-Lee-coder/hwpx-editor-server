"""A fake SDK for the formatting commands, built on behavior measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2.

It emits HWPML2X in the shape captured from Hancom (head, section definition, table and picture text come from a captured
fixture) and models the measured facts: a tab or inline control occupies 8 native positions and any other character one;
reads keep a block selection; SetPos ends one; a position inside a control clears the selection; a sparse action set changes
only its items; CharShape needs a block; ParagraphShape on a block changes each touched paragraph and on a bare caret merges
into the previous caret edit of the same paragraph in the Undo history; a blank paragraph is reachable through the block from
its own start to the next paragraph's start, which leaves that next paragraph alone; Undo gives the selection back and Redo
leaves a caret. Hancom drops definitions nothing uses. The paragraph's last text run decides its printed number mark: a block
reaching the paragraph end also changes a trailing empty run, and a sparse CharShape on the bare caret at the paragraph end
writes that run without touching text, as its own Undo step. Not Hancom evidence.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re
from typing import Any
from xml.sax.saxutils import escape

from tests.native_numbering_fakes import FakeCtrl, chain

_CAPTURED = (Path(__file__).parent / 'fixtures' / 'native_repairs' / 'unique_charshape_before.hwpml2x').read_text(encoding='utf-8')
_BODY_START = _CAPTURED.index('<BODY>')
_BODY_END = _CAPTURED.index('</BODY>')
_SECTION_HEAD = _CAPTURED[_CAPTURED.index('<SECDEF', _BODY_START):_CAPTURED.index('<CHAR>', _CAPTURED.index('</FOOTER>', _BODY_START))]
_PICTURE = re.search(r'<PICTURE .*?</PICTURE>', _CAPTURED, re.S).group(0)
_TABLE = re.search(r'<TABLE .*?</TABLE>', _CAPTURED, re.S).group(0)
_LANGS = ('Hangul', 'Latin', 'Hanja', 'Japanese', 'Other', 'Symbol', 'User')
_ALIGN = ('Justify', 'Left', 'Right', 'Center', 'Distribute', 'DistributeSpace')
_SPACING = ('Percent', 'Fixed', 'BetweenLines', 'AtLeast')
_INLINE = {'\t': '<TAB/>', '\n': '<LINEBREAK/>', ' ': '<NBSPACE/>', ' ': '<FWSPACE/>'}
BASE_FONT = '함초롬바탕'


def char(**changes: Any) -> dict[str, Any]:
    return {'Height': 1000, 'TextColor': 0, 'Bold': False, 'Italic': False, 'Underline': None, 'Font': BASE_FONT, 'Ratio': 100, **changes}


def shape(**changes: Any) -> dict[str, Any]:
    return {'Align': 'Justify', 'Left': 0, 'Right': 0, 'Indent': 0, 'LineSpacing': 160, 'LineSpacingType': 'Percent', 'HeadingType': 'None', 'Level': 0, **changes}


def paragraph(*runs: Any, para_shape: dict[str, Any] | None = None) -> dict[str, Any]:
    """Runs are text, (text, char) pairs, or control atoms from picture()/table()/raw()."""
    atoms: list[dict[str, Any]] = []
    for run in runs:
        if isinstance(run, str):
            run = (run, char())
        if isinstance(run, dict):
            atoms.append({'cs': char(), **run})
            continue
        text, props = run
        atoms.extend({'c': c, 'cs': dict(props)} for c in text)
    return {'atoms': atoms, 'shape': dict(para_shape or shape())}


def end_shape(para: dict[str, Any]) -> dict[str, Any]:
    """The shape the paragraph's last text run holds, which an inherited number mark prints with."""
    if para.get('end') is not None:
        return para['end']
    return para['atoms'][-1]['cs'] if para['atoms'] else char()


def picture() -> dict[str, Any]:
    return {'ctrl': 'gso', 'xml': _PICTURE}


def table(*cells: dict[str, Any]) -> dict[str, Any]:
    assert len(cells) == 4
    return {'ctrl': 'tbl', 'cells': list(cells)}


def textbox(*inner: dict[str, Any]) -> dict[str, Any]:
    """A drawn rectangle holding its own text: a nested list that is not a table cell."""
    return {'ctrl': 'gso', 'drawtext': list(inner)}


def captioned_picture(*inner: dict[str, Any]) -> dict[str, Any]:
    """A picture with a caption: a nested list that is not a table cell."""
    return {'ctrl': 'gso', 'caption': list(inner)}


def width(atom: dict[str, Any]) -> int:
    return 8 if 'ctrl' in atom or atom.get('c') == '\t' else 1


def para_length(para: dict[str, Any]) -> int:
    return sum(width(atom) for atom in para['atoms'])


class _Set:
    def __init__(self) -> None:
        self.items: dict[str, Any] = {}

    def SetItem(self, name: str, value: Any) -> None:  # noqa: N802
        self.items[name] = value


class _Action:
    def __init__(self, hwp: 'FormatHwp', name: str) -> None:
        self.hwp, self.name = hwp, name

    def CreateSet(self) -> _Set:  # noqa: N802
        return _Set()

    def Execute(self, pset: _Set) -> Any:  # noqa: N802
        return self.hwp.execute(self.name, pset.items)


class _HAction:
    def __init__(self, hwp: 'FormatHwp') -> None:
        self.hwp = hwp

    def Run(self, name: str) -> Any:  # noqa: N802
        self.hwp.calls.append(('Run', name))
        if name == 'Undo':
            return self.hwp.history_step(self.hwp.undo_units, self.hwp.redo_units, after_undo=True)
        if name == 'Redo':
            return self.hwp.history_step(self.hwp.redo_units, self.hwp.undo_units, after_undo=False)
        raise AssertionError(f'unexpected HAction.Run({name})')


class FormatHwp:
    def __init__(self, body: list[dict[str, Any]], *, header: str = '머리말 문구', footer: str = '꼬리말 문구', pages: dict[int, int] | None = None) -> None:
        self.body = body
        self.body[0]['atoms'] = [{'ctrl': name, 'cs': char(), 'raw': True} for name in ('secd', 'cold', 'head', 'foot')] + self.body[0]['atoms']
        self.header, self.footer = header, footer
        self.pages = pages or {}
        self.calls: list[tuple] = []
        self.cursor = (0, 0, 32)
        self.selection: tuple[int, ...] | None = None
        self.IsModified = False
        self.PageCount = 2
        self.HAction = _HAction(self)
        self.HParameterSet = object()
        self.undo_units: list[dict[str, Any]] = []
        self.redo_units: list[dict[str, Any]] = []
        self.last_caret_para: tuple[int, int] | None = None
        self.execute_result: Any = True
        self.execute_raises: BaseException | None = None
        self.after_execute = None
        self.collapse_on_block_read = False
        self.position_drift = 0
        self.explicit_number_mark = False
        self.lists = {0: self.body}
        self._number_cells(self.body)

    def _number_cells(self, paragraphs: list[dict[str, Any]]) -> None:
        for para in paragraphs:
            for atom in para['atoms']:
                for name in ('cells', 'drawtext', 'caption'):
                    for inner in atom.get(name, []):
                        self.lists[len(self.lists) + 100] = [inner]
                        self._number_cells([inner])

    # Reads
    @property
    def HeadCtrl(self) -> FakeCtrl | None:  # noqa: N802
        return chain([FakeCtrl(atom['ctrl']) for para in self.body for atom in para['atoms'] if 'ctrl' in atom])

    @property
    def current_page(self) -> int:
        return self.pages.get(self.cursor[1], 1) if self.cursor[0] == 0 else 1

    def get_pos(self) -> tuple[int, int, int]:
        return self.cursor

    def get_selected_pos(self) -> tuple:
        if self.selection is None:
            return (False, None, None, None, None, None, None)
        return (True, *self.selection)

    def _shapes(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        chars, paras, fonts = [char()], [shape()], ['함초롬돋움', BASE_FONT]

        def visit(paragraphs: list[dict[str, Any]]) -> None:
            for para in paragraphs:
                if para['shape'] not in paras:
                    paras.append(para['shape'])
                for atom in para['atoms']:
                    if atom['cs'] not in chars:
                        chars.append(atom['cs'])
                    if atom['cs']['Font'] not in fonts:
                        fonts.append(atom['cs']['Font'])
                    for name in ('cells', 'drawtext', 'caption'):
                        visit(atom.get(name, []))
                if para.get('end') is not None:
                    if para['end'] not in chars:
                        chars.append(para['end'])
                    if para['end']['Font'] not in fonts:
                        fonts.append(para['end']['Font'])

        visit(self.body)
        # Hancom numbers shapes in its own order, so adding one can renumber the shape a definition names.
        if isinstance(self.explicit_number_mark, dict) and self.explicit_number_mark not in chars:
            chars.append(self.explicit_number_mark)
        return chars, paras, fonts

    def _char_xml(self, index: int, props: dict[str, Any], fonts: list[str]) -> str:
        values = lambda value: ' '.join(f'{lang}="{value}"' for lang in sorted(_LANGS))  # noqa: E731
        out = (f'<CHARSHAPE BorderFillId="2" Height="{props["Height"]}" Id="{index}" ShadeColor="4294967295" SymMark="0" TextColor="{props["TextColor"]}" UseFontSpace="false" UseKerning="false">'
               f'<FONTID {values(fonts.index(props["Font"]))}/><RATIO {values(props["Ratio"])}/><CHARSPACING {values(0)}/><RELSIZE {values(100)}/><CHAROFFSET {values(0)}/>')
        if props['Italic']:
            out += '<ITALIC/>'
        if props['Bold']:
            out += '<BOLD/>'
        if props['Underline']:
            out += f'<UNDERLINE Color="{props.get("UnderlineColor", 0)}" Shape="Solid" Type="{props["Underline"]}"/>'
        return out + '</CHARSHAPE>'

    def _para_xml(self, index: int, props: dict[str, Any]) -> str:
        heading = ' Heading="1"' if props['HeadingType'] in {'Number', 'Bullet'} else ''
        return (f'<PARASHAPE Align="{props["Align"]}" AutoSpaceEAsianEng="false" AutoSpaceEAsianNum="false" BreakLatinWord="KeepWord" BreakNonLatinWord="false" Condense="0" FontLineHeight="false"{heading} '
                f'HeadingType="{props["HeadingType"]}" Id="{index}" KeepLines="false" KeepWithNext="false" Level="{props["Level"]}" LineWrap="Break" PageBreakBefore="false" SnapToGrid="true" TabDef="0" VerAlign="Baseline" WidowOrphan="false">'
                f'<PARAMARGIN Indent="{props["Indent"]}" Left="{props["Left"]}" LineSpacing="{props["LineSpacing"]}" LineSpacingType="{props["LineSpacingType"]}" Next="0" Prev="0" Right="{props["Right"]}"/>'
                '<PARABORDER BorderFill="2" Connect="false" IgnoreMargin="false"/></PARASHAPE>')

    def _p(self, para: dict[str, Any], chars: list[dict[str, Any]], paras: list[dict[str, Any]], *, atoms: list[dict[str, Any]] | None = None) -> str:
        whole = atoms is None
        atoms = para['atoms'] if atoms is None else atoms
        out = [f'<P ColumnBreak="false" PageBreak="false" ParaShape="{paras.index(para["shape"])}" Style="0">']
        if not atoms:
            ref = chars.index(para['end']) if para.get('end') is not None else 0
            return out[0] + f'<TEXT CharShape="{ref}"/></P>'
        current, pending, raw_done = None, '', False

        def flush() -> None:
            nonlocal pending
            if pending:
                out.append(f'<CHAR>{pending}</CHAR>')
                pending = ''

        for atom in atoms:
            ref = chars.index(atom['cs'])
            if ref != current:
                flush()
                if current is not None:
                    out.append('</TEXT>')
                out.append(f'<TEXT CharShape="{ref}">')
                current = ref
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

    def _document(self, body: str) -> str:
        chars, paras, fonts = self._shapes()
        faces = ''.join(
            f'<FONTFACE Count="{len(fonts)}" Lang="{lang}">' + ''.join(f'<FONT Id="{i}" Name="{escape(name)}" Type="ttf"/>' for i, name in enumerate(fonts)) + '</FONTFACE>' for lang in _LANGS)
        head = _CAPTURED[:_BODY_START]
        head = re.sub(r'<FACENAMELIST>.*?</FACENAMELIST>', lambda _m: f'<FACENAMELIST>{faces}</FACENAMELIST>', head, flags=re.S)
        head = re.sub(r'<CHARSHAPELIST .*?</CHARSHAPELIST>', lambda _m: f'<CHARSHAPELIST Count="{len(chars)}">' + ''.join(self._char_xml(i, c, fonts) for i, c in enumerate(chars)) + '</CHARSHAPELIST>', head, flags=re.S)
        bullet = '<BULLETLIST Count="1"><BULLET Char="●" Id="1"><PARAHEAD Alignment="Left" AutoIndent="true" TextOffset="50" TextOffsetType="percent" UseInstWidth="false" WidthAdjust="0"/></BULLET></BULLETLIST>'
        head = head.replace('</NUMBERINGLIST>', '</NUMBERINGLIST>' + bullet, 1)
        if self.explicit_number_mark:
            named = chars.index(self.explicit_number_mark) if isinstance(self.explicit_number_mark, dict) else 0
            head = head.replace('<PARAHEAD ', f'<PARAHEAD CharShape="{named}" ')
        head = re.sub(r'<PARASHAPELIST .*?</PARASHAPELIST>', lambda _m: f'<PARASHAPELIST Count="{len(paras)}">' + ''.join(self._para_xml(i, p) for i, p in enumerate(paras)) + '</PARASHAPELIST>', head, flags=re.S)
        return head + f'<BODY><SECTION Id="0">{body}</SECTION>' + _CAPTURED[_BODY_END:]

    def _full_body(self) -> str:
        chars, paras, _fonts = self._shapes()
        parts = [self._p(para, chars, paras) for para in self.body]
        marks = (f'<HEADER ApplyPageType="Both" SeriesNum="0"><PARALIST HasNumRef="false"><P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>{escape(self.header)}</CHAR></TEXT></P></PARALIST></HEADER>',
                 f'<FOOTER ApplyPageType="Both" SeriesNum="0"><PARALIST HasNumRef="false"><P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>{escape(self.footer)}</CHAR></TEXT></P></PARALIST></FOOTER>')
        parts[0] = re.sub(r'<HEADER .*?</HEADER>', lambda _m: marks[0], parts[0], count=1, flags=re.S)
        parts[0] = re.sub(r'<FOOTER .*?</FOOTER>', lambda _m: marks[1], parts[0], count=1, flags=re.S)
        return ''.join(parts)

    def _selected_atoms(self) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        slist, spara, spos, elist, epara, epos = self.selection
        rows = []
        for number in range(spara, epara + 1):
            para = self.lists[slist][number]
            low = spos if number == spara else 0
            high = epos if number == epara else para_length(para)
            kept, at = [], 0
            for atom in para['atoms']:
                if low <= at and at + width(atom) <= high:
                    kept.append(atom)
                at += width(atom)
            rows.append((para, kept))
        return rows

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        self.calls.append(('get_text_file', fmt, option))
        block = 'saveblock' in option
        if block and self.collapse_on_block_read:
            self.selection = None
        if fmt == 'UNICODE':
            return ''
        if not block:
            return self._document(self._full_body())
        if self.selection is None:
            return ''
        chars, paras, _fonts = self._shapes()
        return self._document(''.join(self._p(para, chars, paras, atoms=atoms) for para, atoms in self._selected_atoms()))

    # Navigation
    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        self.calls.append(('set_pos', list_id, para, pos))
        self.selection = None
        self.cursor = (list_id, para, pos)
        return True

    def Cancel(self) -> bool:  # noqa: N802
        self.calls.append(('Cancel',))
        self.selection = None
        return True

    def MoveDocBegin(self) -> None:  # noqa: N802
        self.calls.append(('MoveDocBegin',))
        self.selection = None
        self.cursor = (0, 0, 0)

    def select_text(self, spara: int, spos: int, epara: int, epos: int, slist: int = 0) -> bool:
        self.calls.append(('select_text', spara, spos, epara, epos, slist))
        spos, epos = spos + self.position_drift, epos + self.position_drift
        for para_index, pos in ((spara, spos), (epara, epos)):
            if para_index >= len(self.lists[slist]):
                return False
            at = 0
            for atom in self.lists[slist][para_index]['atoms']:
                if at < pos < at + width(atom):
                    self.selection = None
                    self.cursor = (slist, para_index, pos)
                    return True
                at += width(atom)
        self.selection = (slist, spara, spos, slist, epara, epos)
        self.cursor = (slist, epara, epos)
        return True

    def find(self, src: str, **options: Any) -> bool:
        self.calls.append(('find', src))
        lists = sorted(self.lists)
        for list_id in lists[lists.index(self.cursor[0]):]:
            for number, para in enumerate(self.lists[list_id]):
                if (list_id, number) < self.cursor[:2]:
                    continue
                text, positions, at = '', [], 0
                for atom in para['atoms']:
                    if 'ctrl' not in atom:
                        text += atom['c']
                        positions.append(at)
                    at += width(atom)
                positions.append(at)
                start = self.cursor[2] if (list_id, number) == self.cursor[:2] else 0
                offset = next((i for i in range(len(text)) if positions[i] >= start and text.startswith(src, i)), None)
                if offset is not None:
                    self.selection = (list_id, number, positions[offset], list_id, number, positions[offset + len(src)])
                    self.cursor = (list_id, number, positions[offset + len(src)])
                    return True
        return False

    # Edits
    def CreateAction(self, name: str) -> _Action:  # noqa: N802
        return _Action(self, name)

    def _snapshot(self) -> dict[str, Any]:
        return {'body': copy.deepcopy(self.body), 'selection': self.selection, 'cursor': self.cursor}

    def execute(self, name: str, items: dict[str, Any]) -> Any:
        self.calls.append(('Execute', name, dict(items), self.selection))
        if self.execute_raises is not None:
            raise self.execute_raises
        if self.execute_result is not True:
            return self.execute_result
        before = self._snapshot()
        merge = False
        if name == 'CharShape':
            self._apply_char(items)
        elif name == 'ParagraphShape':
            caret = (self.cursor[0], self.cursor[1])
            merge = self.selection is None and caret == self.last_caret_para
            self._apply_para(items)
            self.last_caret_para = caret if self.selection is None else None
        else:
            raise AssertionError(f'unexpected action {name}')
        if name != 'ParagraphShape':
            self.last_caret_para = None
        if not merge:
            self.undo_units.append({'state': before, 'selection': self.selection, 'cursor': self.cursor})
        self.redo_units = []
        self.IsModified = True
        if self.after_execute is not None:
            self.after_execute(self)
        return True

    @staticmethod
    def _char_with(props: dict[str, Any], items: dict[str, Any]) -> dict[str, Any]:
        changed = dict(props)
        for key, value in items.items():
            if key == 'Height':
                changed['Height'] = value
            elif key == 'TextColor':
                changed['TextColor'] = value
            elif key in {'Bold', 'Italic'}:
                changed[key] = bool(value)
            elif key == 'UnderlineType':
                changed['Underline'] = {0: None, 1: 'Bottom', 3: 'Top'}[value]
            elif key == 'FaceNameHangul':
                changed['Font'] = value
            elif not key.startswith(('FaceName', 'FontType')):
                raise AssertionError(f'unexpected CharShape item {key}')
        return changed

    def _apply_char(self, items: dict[str, Any]) -> None:
        if self.selection is None:
            para = self.lists[self.cursor[0]][self.cursor[1]]
            if self.cursor[2] != para_length(para):
                raise AssertionError('caret-wide character formatting is never issued')
            para['end'] = self._char_with(end_shape(para), items)
            return
        slist, spara, _spos, _elist, epara, epos = self.selection
        for number, (para, atoms) in enumerate(self._selected_atoms(), start=spara):
            old_end = para.get('end')
            for atom in atoms:
                atom['cs'] = self._char_with(atom['cs'], items)
            # A block starting right after a section's leading controls takes them into the same run (measured).
            lead = [atom for atom in para['atoms'] if atom.get('raw')]
            if lead and atoms and para['atoms'][len(lead)] is atoms[0]:
                for atom in lead:
                    atom['cs'] = self._char_with(atom['cs'], items)
            if old_end is not None and (number < epara or epos == para_length(para)):
                para['end'] = self._char_with(old_end, items)

    def _apply_para(self, items: dict[str, Any]) -> None:
        if self.selection is None:
            targets = [self.lists[self.cursor[0]][self.cursor[1]]]
        else:
            slist, spara, _spos, _elist, epara, epos = self.selection
            # A block ending at a next paragraph's start leaves that paragraph alone.
            last = epara - 1 if epos == 0 and epara > spara else epara
            targets = self.lists[slist][spara:last + 1]
        names = {'LeftMargin': 'Left', 'RightMargin': 'Right', 'Indentation': 'Indent', 'LineSpacing': 'LineSpacing'}
        for para in targets:
            props = dict(para['shape'])
            for key, value in items.items():
                if key == 'AlignType':
                    props['Align'] = _ALIGN[value]
                elif key == 'LineSpacingType':
                    props['LineSpacingType'] = _SPACING[value]
                elif key in names:
                    props[names[key]] = value
                else:
                    raise AssertionError(f'unexpected ParagraphShape item {key}')
            para['shape'] = props

    def history_step(self, source: list[dict[str, Any]], target: list[dict[str, Any]], *, after_undo: bool) -> bool:
        if not source:
            return False
        unit = source.pop()
        target.append({'state': self._snapshot(), 'selection': unit['selection'], 'cursor': unit['cursor']})
        self.body = copy.deepcopy(unit['state']['body'])
        self.lists = {0: self.body}
        self._number_cells(self.body)
        if after_undo:
            self.selection, self.cursor = unit['selection'], unit['cursor']
        else:
            self.selection = None
            start = unit['state']['selection']
            self.cursor = (start[0], start[1], start[2]) if start else unit['state']['cursor']
        self.last_caret_para = None
        self.IsModified = True
        return True

    # Inspection for tests
    def executes(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == 'Execute']

    def atoms_of(self, list_id: int, para: int) -> list[dict[str, Any]]:
        return self.lists[list_id][para]['atoms']

    def text_props(self, list_id: int, para: int) -> list[tuple[str, dict[str, Any]]]:
        return [(atom['c'], atom['cs']) for atom in self.atoms_of(list_id, para) if 'c' in atom]


def sample_document(**options: Any) -> FormatHwp:
    red = char(Height=1400, TextColor=192, Bold=True)
    blue = char(Height=1200, TextColor=12582912)
    body = [
        paragraph('제목 문단'),
        paragraph(('굵은빨강 문단', red)),
        paragraph('혼합 앞 일반 ', ('빨강구간 Bold ', red), ('파랑구간 ', blue), '뒤 일반'),
        paragraph('한글과 English 혼합 target phrase 끝'),
        paragraph('중복문구 첫째'),
        paragraph('중복문구 둘째'),
        paragraph('공백  두칸  문구 끝'),
        paragraph('탭앞글자\t탭뒤글자\n줄뒤 묶음 고정'),
        paragraph(table(paragraph('셀 표적 문구 뒤'), paragraph('셀중복'), paragraph('셀중복'), paragraph(''))),
        paragraph('그림앞', picture(), '그림뒤 문구'),
        paragraph(table(paragraph('내부 A1'), paragraph('내부 B1'), paragraph('내부 A2'), paragraph('내부 B2')), '표뒤 글자'),
        paragraph('앞 이웃 문단'),
        paragraph('문단 대상'),
        paragraph('뒤 이웃 문단'),
        paragraph('범위 첫째'),
        paragraph('범위 둘째'),
        paragraph('범위 셋째'),
        paragraph(''),
        paragraph('빈문단 뒤'),
        paragraph('번호 문단', para_shape=shape(HeadingType='Number')),
        paragraph(textbox(paragraph('글상자 안 문구'))),
        paragraph(table(paragraph('전용표 A1'), paragraph('전용표 B1'), paragraph('전용표 A2'), paragraph('전용표 B2'))),
        paragraph('마지막 문단'),
    ]
    return FormatHwp(body, **options)


def numbered_document(**options: Any) -> FormatHwp:
    """Numbered paragraphs for what a range reaching the paragraph end does; the indexes are fixed for the tests."""
    number, outline, bullet = shape(HeadingType='Number'), shape(HeadingType='Outline'), shape(HeadingType='Bullet')
    body = [
        paragraph('제목 문단'),                            # 0
        paragraph('번호 앞글 꼬리글', para_shape=number),   # 1
        paragraph('개요 앞글 꼬리글', para_shape=outline),  # 2
        paragraph('글머 앞글 꼬리글', para_shape=bullet),   # 3
        paragraph('일반 앞글 꼬리글'),                      # 4
        paragraph('위쪽 앞글 ', ('꼬리글', char(Underline='Top')), para_shape=number),   # 5
        paragraph('걸침 위글 꼬리글', para_shape=number),   # 6
        paragraph('걸침 아래글 끝글', para_shape=number),   # 7
        paragraph('굵끝 앞글 ', ('꼬리글', char(Bold=True)), para_shape=number),   # 8
        paragraph('그림 앞글', picture(), para_shape=number),   # 9
        paragraph(table(paragraph('셀번 앞글 꼬리글', para_shape=number), paragraph('칸둘'), paragraph('칸셋'), paragraph('칸넷'))),  # 10
        paragraph('마지막 문단'),                           # 11
    ]
    return FormatHwp(body, **options)
