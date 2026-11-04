"""Fakes for replacing existing text and filling table cells, on top of the formatting and table fakes.

They model what was measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2 on disposable synthetic documents: InsertText
at a bare caret gives the new text the character shape of the character to its left (a blank paragraph's own empty run when
there is none); InsertText over a block replaces it and takes the shape of the character before the block; Delete on a
block removes exactly its characters and leaves the caret at the block's start; each of these is one Undo step, so text
inserted at a range's end and the old range deleted after it are two. A bookmark is inserted at the caret by one Execute;
an equation too, and Redo of an equation brings it back with a new z-order, as measured. Not Hancom evidence.
"""
from __future__ import annotations

import copy
import re
from typing import Any
from xml.sax.saxutils import escape

from tests.native_format_fakes import FormatHwp, _HAction as _FormatHAction, char, end_shape, para_length, width
from tests.native_table_fakes import TableHwp, para


def _atom_index(para_dict: dict[str, Any], pos: int) -> int:
    at = 0
    for number, atom in enumerate(para_dict['atoms']):
        if at == pos:
            return number
        at += width(atom)
    if at == pos:
        return len(para_dict['atoms'])
    raise AssertionError(f'position {pos} is not between atoms')


class _ParamSet:
    """A parameter set reached as HParameterSet.<name>: items through HSet.SetItem, or as attributes (pset.string)."""

    def __init__(self) -> None:
        object.__setattr__(self, 'items', {})
        object.__setattr__(self, 'HSet', self)

    def SetItem(self, name: str, value: Any) -> None:  # noqa: N802
        self.items[name] = value

    def __setattr__(self, name: str, value: Any) -> None:
        self.items[name] = value


class _Params:
    def __init__(self) -> None:
        self.HBookMark = _ParamSet()
        self.HEqEdit = _ParamSet()


class _TextHAction(_FormatHAction):
    def GetDefault(self, name: str, hset: Any) -> bool:  # noqa: N802
        self.hwp.calls.append(('GetDefault', name))
        hset.items.clear()
        return True

    def Execute(self, name: str, hset: Any) -> Any:  # noqa: N802
        return self.hwp.execute(name, dict(hset.items))

    def Run(self, name: str) -> Any:  # noqa: N802
        if name == 'Delete':
            self.hwp.calls.append(('Run', name))
            return self.hwp.delete_block()
        if name == 'Cancel':
            self.hwp.calls.append(('Run', name))
            self.hwp.selection = None
            return True
        return super().Run(name)


class TextHwp(FormatHwp):
    def __init__(self, body: list[dict[str, Any]], **options: Any) -> None:
        super().__init__(body, **options)
        self.HAction = _TextHAction(self)
        self.HParameterSet = _Params()
        self.insert_result: Any = True
        self.delete_result: Any = True
        self.insert_shape: dict[str, Any] | None = None  # A wrong outcome: the new text takes this shape.
        self.delete_extra = 0  # A wrong outcome: Delete also takes this many atoms after the block.
        self.renew_on_redo = False
        self.next_inst = 900001

    @property
    def SelectionMode(self) -> int:  # noqa: N802
        return 1 if self.selection is not None else 0

    def GetTextFile(self, fmt: str, option: str) -> str:  # noqa: N802
        return self.get_text_file(fmt, option)

    def _unit(self, before: dict[str, Any]) -> None:
        self.undo_units.append({'state': before, 'selection': self.selection, 'cursor': self.cursor})
        self.redo_units = []
        self.IsModified = True
        self.last_caret_para = None
        if self.after_execute is not None:
            self.after_execute(self)

    def _insert_atoms(self, atoms: list[dict[str, Any]]) -> None:
        list_id, number, pos = self.cursor
        target = self.lists[list_id][number]
        index = _atom_index(target, pos)
        target['atoms'][index:index] = atoms
        self.cursor = (list_id, number, pos + sum(width(atom) for atom in atoms))

    def _left_shape(self) -> dict[str, Any]:
        list_id, number, pos = self.cursor
        target = self.lists[list_id][number]
        index = _atom_index(target, pos)
        if index:
            return dict(target['atoms'][index - 1]['cs'])
        return dict(end_shape(target)) if not target['atoms'] else dict(target['atoms'][0]['cs'])

    def execute(self, name: str, items: dict[str, Any]) -> Any:
        if name not in {'InsertText', 'Bookmark', 'EquationCreate'}:
            return super().execute(name, items)
        self.calls.append(('Execute', name, dict(items), self.selection))
        if self.execute_raises is not None:
            raise self.execute_raises
        result = self.insert_result if name == 'InsertText' else self.execute_result
        if result is not True:
            return result
        before = self._snapshot()
        if name == 'InsertText':
            if self.selection is not None:
                slist, spara, spos, _elist, _epara, _epos = self.selection
                self._remove_selected(0)
                self.cursor = (slist, spara, spos)
            shape = self.insert_shape or self._left_shape()
            self._insert_atoms([{'c': c, 'cs': dict(shape)} for c in items['Text']])
        elif name == 'Bookmark':
            self._insert_atoms([{'ctrl': 'bokm', 'xml': f'<BOOKMARK Name="{escape(items["Name"])}"/>', 'joins': True, 'cs': self._left_shape()}])
        else:
            inst, self.next_inst = self.next_inst, self.next_inst + 1
            xml = (f'<EQUATION BaseLine="85" BaseUnit="1000" LineMode="false" TextColor="0" Version="Equation Version 60"><SHAPEOBJECT InstId="{inst}" Lock="false" '
                   'NumberingType="Equation" TextFlow="BothSides" TextWrap="TopAndBottom" ZOrder="7"><SIZE Height="1000" HeightRelTo="Absolute" Protect="false" Width="2000" '
                   'WidthRelTo="Absolute"/><POSITION AffectLSpacing="false" AllowOverlap="false" FlowWithText="true" HoldAnchorAndSO="false" HorzAlign="Left" HorzOffset="0" '
                   f'HorzRelTo="Para" TreatAsChar="true" VertAlign="Top" VertOffset="0" VertRelTo="Para"/></SHAPEOBJECT><SCRIPT>{escape(items.get("string", ""))}</SCRIPT></EQUATION>')
            self._insert_atoms([{'ctrl': 'eqed', 'xml': xml, 'cs': self._left_shape()}])
        self.selection = None
        self._unit(before)
        return True

    def _remove_selected(self, extra: int) -> None:
        slist, spara, spos, _elist, epara, epos = self.selection
        assert spara == epara, 'a block over one paragraph only'
        target = self.lists[slist][spara]
        low, high = _atom_index(target, spos), _atom_index(target, epos)
        del target['atoms'][low:high + extra]

    def delete_block(self) -> Any:
        if self.selection is None:
            raise AssertionError('Delete without a block is never issued')
        if self.delete_result is not True:
            return self.delete_result
        before = self._snapshot()
        slist, spara, spos, _elist, _epara, _epos = self.selection
        self._remove_selected(self.delete_extra)
        self.selection = None
        self.cursor = (slist, spara, spos)
        self._unit(before)
        return True

    def history_step(self, source: list[dict[str, Any]], target: list[dict[str, Any]], *, after_undo: bool) -> bool:
        done = super().history_step(source, target, after_undo=after_undo)
        if done and not after_undo and self.renew_on_redo:
            # Measured: Redo brings an equation back with a new z-order.
            for paragraph in self.body:
                for atom in paragraph['atoms']:
                    if atom.get('ctrl') == 'eqed':
                        atom['xml'] = re.sub(r'ZOrder="(\d+)"', lambda m: f'ZOrder="{int(m.group(1)) + 1}"', atom['xml'])
        return done


def plan_document(**options: Any) -> TextHwp:
    """The paragraph indexes the tests name are fixed: see the comments."""
    from tests.native_format_fakes import bookmark, paragraph, picture, shape, table, textbox
    bold = char(Bold=True)
    title = char(Height=1600, Bold=True)
    body = [
        paragraph(('Annual Plan 2025', title)),  # 0, after the section, column, header and footer controls
        paragraph('This document sets out the annual plan for 2025. The ', ('budget', bold), ' and the ', ('schedule', bold), ' are summarized below.'),  # 1
        paragraph(table(paragraph('Materials'), paragraph('1,200'), paragraph('Planned'), paragraph(''))),  # 2, cells 3..6
        paragraph('Status: draft. Owner: team. Status: draft.'),  # 7
        paragraph('Status: draft'),  # 8
        paragraph('Mixed ', ('Red', char(TextColor=192, Bold=True)), ('Blue', char(Italic=True)), ' tail'),  # 9
        paragraph('Before', picture(), 'Text after the picture'),  # 10
        paragraph('Before mark', bookmark('mark'), ' after'),  # 11
        paragraph('First numbered item', para_shape=shape(HeadingType='Number')),  # 12
        paragraph(textbox(paragraph('Inside box text'))),  # 13, box 14
        paragraph('Last paragraph'),  # 15
    ]
    return TextHwp(body, header='SAMPLE REPORT', footer='Sample document', **options)


class CellTextHwp(TableHwp):
    """The table fake with a text caret and text blocks inside cells, InsertText and Delete."""

    def __init__(self, body: list[Any], **options: Any) -> None:
        super().__init__(body, **options)
        self.caret_cell = None
        self.caret = (0, 0)
        self.text_block: tuple[int, ...] | None = None
        self.insert_result: Any = True
        self.delete_result: Any = True
        self.insert_shape: int | None = None

    @property
    def SelectionMode(self) -> int:  # noqa: N802
        return 1 if self.text_block is not None else (19 if self.block else 0)

    def get_pos(self) -> tuple[int, int, int]:
        if self.cell is not None and self.caret_cell is self.cell:
            return (self._list_of(self.cell), *self.caret)
        return super().get_pos()

    def get_selected_pos(self) -> tuple:
        return (True, *self.text_block) if self.text_block is not None else super().get_selected_pos()

    def set_pos(self, list_id: int, para_index: int, pos: int) -> bool:
        done = super().set_pos(list_id, para_index, pos)
        self.text_block = None
        if list_id != 0:
            self.caret_cell, self.caret = self.cell, (para_index, pos)
        return done

    def select_text(self, spara: int, spos: int, epara: int, epos: int, slist: int = 0) -> bool:
        self.calls.append(('select_text', spara, spos, epara, epos, slist))
        assert slist != 0 and spara == epara
        self.table, self.cell = self._lists()[slist]
        self.block = None
        text = self._chars(self.cell.paragraphs[spara])
        if not 0 <= spos <= epos <= len(text):
            return False
        self.text_block = (slist, spara, spos, slist, epara, epos)
        self.caret_cell, self.caret = self.cell, (epara, epos)
        return True

    def run(self, name: str) -> Any:
        if name == 'Cancel':
            self.text_block = None
        if name == 'Delete':
            self.calls.append(('Run', name))
            return self._delete()
        if name in {'TableColBegin', 'TableColPageUp', 'TableRightCell', 'TableLowerCell', 'ShapeObjTextBoxEdit'}:
            self.text_block = None
        return super().run(name)

    @staticmethod
    def _chars(item: dict[str, Any]) -> list[tuple[str, int]]:
        chars: list[tuple[str, int]] = []
        for run in item['runs']:
            assert isinstance(run, tuple), 'text runs only'
            chars.extend((c, run[1]) for c in run[0])
        return chars

    @staticmethod
    def _runs(chars: list[tuple[str, int]]) -> list[tuple[str, int]]:
        runs: list[tuple[str, int]] = []
        for c, shape in chars:
            if runs and runs[-1][1] == shape:
                runs[-1] = (runs[-1][0] + c, shape)
            else:
                runs.append((c, shape))
        return runs

    def _edit(self) -> tuple[dict[str, Any], list[tuple[str, int]]]:
        self.undo_stack.append(copy.deepcopy(self.body))
        self.redo_stack = []
        self.IsModified = True
        item = self.cell.paragraphs[self.caret[0]]
        return item, self._chars(item)

    def native(self, name: str, items: dict[str, Any], *, sparse: bool) -> Any:
        if name != 'InsertText':
            return super().native(name, items, sparse=sparse)
        self.calls.append(('native', name, items, self.cell.address if self.cell is not None else None))
        if self.insert_result is not True:
            return self.insert_result
        assert self.caret_cell is self.cell and self.text_block is None, 'InsertText at a caret in a cell'
        item, chars = self._edit()
        at = self.caret[1]
        shape = self.insert_shape if self.insert_shape is not None else (chars[at - 1][1] if at else 0)
        chars[at:at] = [(c, shape) for c in items['Text']]
        item['runs'] = self._runs(chars)
        self.caret = (self.caret[0], at + len(items['Text']))
        return True

    def _delete(self) -> Any:
        if self.text_block is None:
            raise AssertionError('Delete without a block is never issued')
        if self.delete_result is not True:
            return self.delete_result
        _slist, spara, spos, _elist, _epara, epos = self.text_block
        self.caret = (spara, spos)
        item, chars = self._edit()
        del chars[spos:epos]
        item['runs'] = self._runs(chars)
        self.text_block = None
        return True

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        if fmt == 'HWPML2X' and 'saveblock' in option:
            self.calls.append(('get_text_file', fmt, option))
            if self.text_block is None:
                return ''
            _slist, spara, spos, _elist, _epara, epos = self.text_block
            chosen = self._chars(self.cell.paragraphs[spara])[spos:epos]
            body, self.body = self.body, [{'runs': self._runs(chosen)}]
            try:
                return super().get_text_file(fmt, '')
            finally:
                self.body = body
                self.calls.pop()
        return super().get_text_file(fmt, option)


def plan_tables() -> CellTextHwp:
    from tests.native_table_fakes import grid
    return CellTextHwp([
        'Annual Plan 2025',
        'This document sets out the annual plan for 2025.',
        grid('1011', [['Item', 'Amount', 'Note'], ['Materials', '1,200', 'Planned'], ['Printing', '300', 'Unchanged']]),
        grid('1012', [[{'paragraphs': [para('합친머리')], 'cs': 2}, None, ''],
                      ['A2', {'paragraphs': [para('두'), para('문단')]}, {'paragraphs': [para(('굵게', 1), ('보통', 0))]}],
                      [{'paragraphs': [para('picture', '그림뒤')]}, 'B3', 'C3']]),
        '맺음 문단',
    ])
