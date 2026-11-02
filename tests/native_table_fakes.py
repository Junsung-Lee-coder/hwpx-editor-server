"""A fake SDK for the table commands, built on behavior measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2.

It emits HWPML2X in the shape captured from Hancom and models what was measured on synthetic documents: a cell's list id follows
its position; set_pos ends a block; a block grows from its first cell by TableRightCell and TableLowerCell, which go to whatever
cell occupies the next slot, and a cell it touches is taken whole; a sparse CellBorder treats BorderType* as the outer edges of
the block and TypeHorz/TypeVert as its inner lines, writes the shared side of a neighbour only when that neighbour's whole side
lies on the edge, and ignores BorderColorLeft because the item is spelled BorderCorlorLeft; a sparse CellFill does nothing and the
GetDefault set writes the fill alone; TableInsertRowColumn makes all its lines in one action, mirroring the source line; a new line
past the table's edge takes the frame, and the old line's side there takes the value of its other side; a deleted line at the table's
edge hands its frame to the line that becomes the edge; TableDeleteRow on a block does nothing while TableDeleteCell on a whole-line block removes the lines; a merge joins
the paragraphs in reading order, drops a cell that is one blank paragraph, takes its left and top lines and fill from the first cell
and its right and bottom lines from the far corner cell, and collapses a line no cell starts on; a split leaves everything in the first
part and divides width equally, and a later part's left line is the right line and its top line the bottom line of the split cell. One native edit is one Undo.
Not Hancom evidence.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re
from typing import Any
from xml.sax.saxutils import escape

_CAPTURED = (Path(__file__).parent / 'fixtures' / 'native_repairs' / 'unique_charshape_before.hwpml2x').read_text(encoding='utf-8')
_BODY_START = _CAPTURED.index('<BODY>')
_BODY_END = _CAPTURED.index('</BODY>')
_SECTION_HEAD = _CAPTURED[_CAPTURED.index('<SECDEF', _BODY_START):_CAPTURED.index('<CHAR>', _CAPTURED.index('</FOOTER>', _BODY_START))]
_PICTURE = re.search(r'<PICTURE .*?</PICTURE>', _CAPTURED, re.S).group(0)
SOLID = ('Solid', '0.12mm', 0)
SIDES = ('left', 'right', 'top', 'bottom')
_OUTER_SIDE = {0: 'left', 1: 'right', 2: 'top', 3: 'bottom'}
_OPPOSITE = {'left': 'right', 'right': 'left', 'top': 'bottom', 'bottom': 'top'}
LINE_NAMES = {0: 'None', 1: 'Solid', 2: 'Dash', 3: 'Dot', 4: 'DashDot', 5: 'DashDotDot', 6: 'LongDash', 7: 'Circle', 8: 'DoubleSlim', 9: 'SlimThick', 10: 'ThickSlim', 11: 'SlimThickSlim'}
WIDTH_NAMES = ('0.1mm', '0.12mm', '0.15mm', '0.2mm', '0.25mm', '0.3mm', '0.4mm', '0.5mm', '0.6mm', '0.7mm', '1.0mm', '1.5mm', '2.0mm', '3.0mm', '4.0mm', '5.0mm')


def letters(col: int) -> str:
    out = ''
    while col:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return out


def para(*runs: Any) -> dict[str, Any]:
    """Runs are text, (text, char shape index) pairs, or a nested table / 'picture' object."""
    return {'runs': [run if isinstance(run, (tuple, Table)) or run == 'picture' else (run, 0) for run in runs]}


class Cell:
    def __init__(self, row: int, col: int, *paragraphs: dict[str, Any], rs: int = 1, cs: int = 1, width: int = 9000, height: int = 1800) -> None:
        self.row, self.col, self.rs, self.cs, self.width, self.height = row, col, rs, cs, width, height
        self.paragraphs = list(paragraphs) or [para()]
        self.border = {side: SOLID for side in SIDES}
        self.fill: Any = None

    @property
    def address(self) -> str:
        return f'{letters(self.col)}{self.row}'

    def covers(self, row: int, col: int) -> bool:
        return self.row <= row < self.row + self.rs and self.col <= col < self.col + self.cs

    def blank(self) -> bool:
        return len(self.paragraphs) == 1 and not any(item == 'picture' or isinstance(item, Table) or item[0] for item in self.paragraphs[0]['runs'])


class Table:
    def __init__(self, inst: str, rows: int, cols: int, cells: list[Cell]) -> None:
        self.inst, self.rows, self.cols, self.cells = inst, rows, cols, cells

    def at(self, row: int, col: int) -> Cell | None:
        return next((cell for cell in self.cells if cell.covers(row, col)), None)

    def named(self, address: str) -> Cell:
        return next(cell for cell in self.cells if cell.address == address)

    def ordered(self) -> list[Cell]:
        return sorted(self.cells, key=lambda cell: (cell.row, cell.col))


def grid(inst: str, rows: list[list[Any]], *, widths: list[int] | None = None) -> Table:
    """Rows of cell text, a Cell-building dict (paragraphs, rs, cs, fill) or None for a covered slot."""
    cells: list[Cell] = []
    for r, row in enumerate(rows, start=1):
        for c, spec in enumerate(row, start=1):
            if spec is None:
                continue
            spec = {'paragraphs': [para(spec)] if spec else [para()]} if isinstance(spec, str) else spec
            cs = spec.get('cs', 1)
            width = sum(widths[c - 1:c - 1 + cs]) if widths else 9000 * cs
            cell = Cell(r, c, *spec['paragraphs'], rs=spec.get('rs', 1), cs=cs, width=width, height=1800 * spec.get('rs', 1))
            cell.fill = spec.get('fill')
            cells.append(cell)
    return Table(inst, len(rows), max(len(row) for row in rows), cells)


class _Set:
    def __init__(self) -> None:
        self.items: dict[str, Any] = {}

    def SetItem(self, name: str, value: Any) -> None:  # noqa: N802
        self.items[name] = value

    def CreateItemSet(self, name: str, _kind: str) -> '_Set':  # noqa: N802
        inner = _Set()
        self.items[name] = inner
        return inner


class _Action:
    def __init__(self, hwp: 'TableHwp', name: str) -> None:
        self.hwp, self.name = hwp, name

    def CreateSet(self) -> _Set:  # noqa: N802
        return _Set()

    def Execute(self, pset: _Set) -> Any:  # noqa: N802
        return self.hwp.native(self.name, dict(pset.items), sparse=True)


class _FillAttr:
    def __init__(self) -> None:
        self.type, self.WinBrushFaceColor, self.WinBrushHatchColor, self.WinBrushFaceStyle, self.WindowsBrush = 0, 0, 0, 0, 0


class _CellSet:
    def __init__(self) -> None:
        self.HSet = self
        self.FillAttr = _FillAttr()


class _LineSet:
    def __init__(self) -> None:
        self.HSet = self
        self.Side, self.Count = 0, 1


class _Params:
    def __init__(self) -> None:
        self.HCellBorderFill, self.HTableInsertLine = _CellSet(), _LineSet()


class _HAction:
    def __init__(self, hwp: 'TableHwp') -> None:
        self.hwp = hwp

    def GetDefault(self, name: str, hset: Any) -> bool:  # noqa: N802
        self.hwp.calls.append(('GetDefault', name))
        return True

    def Execute(self, name: str, hset: Any) -> Any:  # noqa: N802
        if name == 'CellFill':
            attr = hset.FillAttr
            return self.hwp.native(name, {'type': attr.type, 'face': attr.WinBrushFaceColor, 'hatch': attr.WinBrushHatchColor}, sparse=False)
        if name == 'TableInsertRowColumn':
            return self.hwp.native(name, {'Side': hset.Side, 'Count': hset.Count}, sparse=False)
        raise AssertionError(f'unexpected HAction.Execute({name})')

    def Run(self, name: str) -> Any:  # noqa: N802
        return self.hwp.run(name)


class FakeAnchor:
    def __init__(self, table: Table) -> None:
        self.table = table


class FakeTableCtrl:
    CtrlID = 'tbl'

    def __init__(self, table: Table) -> None:
        self.table = table
        self.Next: Any = None

    def GetCtrlInstID(self) -> str:  # noqa: N802
        return self.table.inst

    def GetAnchorPos(self, _option: int) -> FakeAnchor:  # noqa: N802
        return FakeAnchor(self.table)


class TableHwp:
    def __init__(self, body: list[Any], *, pages: dict[str, int] | None = None) -> None:
        self.body = body  # paragraphs (text or para dict) and Tables; a Table holds its own body paragraph.
        self.pages = pages or {}
        self.calls: list[tuple] = []
        self.cell: Cell | None = None
        self.table: Table | None = None
        self.block: tuple[Cell, Cell] | None = None
        self.body_caret = (0, 0, 0)
        self.selected_ctrl: Table | None = None
        self.IsModified = False
        self.PageCount = 2
        self.HAction = _HAction(self)
        self.HParameterSet = _Params()
        self.undo_stack: list[Any] = []
        self.redo_stack: list[Any] = []
        self.corrupt_after_execute: Any = None
        self.execute_result: Any = True
        self.execute_raises: BaseException | None = None
        self.selection_off_by_one = False
        self.fail_action: tuple[str, int] | None = None
        self.message_box: list[int] = []

    # What the document is.
    def tables(self) -> list[Table]:
        found: list[Table] = []

        def visit_para(item: dict[str, Any]) -> None:
            for run in item['runs']:
                if isinstance(run, Table):
                    visit_table(run)

        def visit_table(table: Table) -> None:
            found.append(table)
            for cell in table.ordered():
                for item in cell.paragraphs:
                    visit_para(item)

        for item in self.body:
            if isinstance(item, Table):
                visit_table(item)
            elif isinstance(item, dict):
                visit_para(item)
        return found

    def by_inst(self, inst: str) -> Table:
        return next(table for table in self.tables() if table.inst == inst)

    def _lists(self) -> dict[int, tuple[Table, Cell]]:
        lists: dict[int, tuple[Table, Cell]] = {}
        for table in self.tables():
            for cell in table.ordered():
                lists[len(lists) + 1] = (table, cell)
        return lists

    def _list_of(self, cell: Cell) -> int:
        return next(number for number, (_table, item) in self._lists().items() if item is cell)

    # Reads.
    @property
    def HeadCtrl(self) -> Any:  # noqa: N802
        controls = [FakeTableCtrl(table) for table in self.tables()]
        for left, right in zip(controls, controls[1:]):
            left.Next = right
        return controls[0] if controls else None

    @property
    def ParentCtrl(self) -> Any:  # noqa: N802
        return FakeTableCtrl(self.table) if self.table is not None else None

    @property
    def SelectionMode(self) -> int:  # noqa: N802
        return 19 if self.block else 0

    @property
    def current_page(self) -> int:
        return self.pages.get(self.table.inst, 1) if self.table is not None else self._anchor_page

    _anchor_page = 1
    _walk_row = 1

    def is_cell(self) -> bool:
        return self.cell is not None

    def get_cell_addr(self, as_: str = 'str') -> Any:
        return self.cell.address if self.cell is not None else False

    def get_pos(self) -> tuple[int, int, int]:
        return (self._list_of(self.cell), 0, 0) if self.cell is not None else self.body_caret

    def get_selected_pos(self) -> tuple:
        # A cell block is not a text selection through this getter.
        return (False, None, None, None, None, None, None)

    def _block_cells(self) -> list[Cell]:
        first, last = self.block
        table = self.table
        top, bottom = min(first.row, last.row), max(first.row + first.rs, last.row + last.rs) - 1
        left, right = min(first.col, last.col), max(first.col + first.cs, last.col + last.cs) - 1
        while True:
            touched = [cell for cell in table.cells if cell.row <= bottom and cell.row + cell.rs - 1 >= top and cell.col <= right and cell.col + cell.cs - 1 >= left]
            grown = (min(c.row for c in touched), max(c.row + c.rs for c in touched) - 1, min(c.col for c in touched), max(c.col + c.cs for c in touched) - 1)
            if grown == (top, bottom, left, right):
                return sorted(touched, key=lambda cell: (cell.row, cell.col))
            top, bottom, left, right = grown

    def get_selected_range(self) -> list[str]:
        self.calls.append(('get_selected_range',))
        if not self.is_cell():
            raise AttributeError('not in a table')
        if not self.block:
            return ['']
        cells = [cell.address for cell in self._block_cells()]
        return cells[:-1] if self.selection_off_by_one and len(cells) > 1 else cells

    # Moves.
    def set_pos(self, list_id: int, para_index: int, pos: int) -> bool:
        self.calls.append(('set_pos', list_id, para_index, pos))
        self.block = None
        if list_id == 0:
            self.cell, self.table = None, None
            self.body_caret = (0, para_index, pos)
            return True
        self.table, self.cell = self._lists()[list_id]
        return True

    def set_pos_by_set(self, anchor: FakeAnchor) -> bool:
        self.calls.append(('set_pos_by_set', anchor.table.inst))
        self.block = None
        self.cell, self.table = None, None
        self.selected_ctrl = None
        self._anchor_page = self.pages.get(anchor.table.inst, 1)
        self._anchor = anchor.table
        return True

    def FindCtrl(self) -> bool:  # noqa: N802
        self.selected_ctrl = self._anchor
        return True

    def run(self, name: str) -> Any:
        self.calls.append(('Run', name))
        if name == 'ShapeObjTextBoxEdit':
            self.table = self.selected_ctrl
            self.cell = self.table.ordered()[0]
            self.block = None
            return True
        if name == 'Cancel':
            self.block = None
            return True
        if name == 'TableColBegin':
            self.cell = self.table.at(self.cell.row, 1)
            return True
        if name == 'TableColPageUp':
            self.cell = self.table.at(1, self.cell.col)
            return True
        if name == 'TableRightCell':
            # Measured: a walk re-enters a tall cell once per row it covers and goes on along that row.
            row = self._walk_row if self.cell.row <= self._walk_row < self.cell.row + self.cell.rs else self.cell.row
            return self._move(row, self.cell.col + self.cell.cs)
        if name == 'TableLowerCell':
            return self._move(self.cell.row + self.cell.rs, self.cell.col)
        if name == 'TableCellBlock':
            self.block = (self.cell, self.cell)
            return True
        if name == 'TableCellBlockExtend':
            return True
        if name in {'TableMergeCell', 'TableDeleteRow', 'TableDeleteColumn'}:
            return self.native(name, {}, sparse=False)
        if name == 'Undo':
            return self._history(self.undo_stack, self.redo_stack)
        if name == 'Redo':
            return self._history(self.redo_stack, self.undo_stack)
        raise AssertionError(f'unexpected Run({name})')

    def _move(self, row: int, col: int) -> bool:
        table = self.table
        if col > table.cols:
            row, col = row + 1, 1
        if row > table.rows:
            return False
        self.cell = table.at(row, col)
        self._walk_row = row
        if self.block:
            self.block = (self.block[0], self.cell)
        return True

    def _history(self, source: list[Any], target: list[Any]) -> bool:
        if not source:
            return False
        address = self.cell.address if self.cell is not None else None
        inst = self.table.inst if self.table is not None else None
        target.append(copy.deepcopy(self.body))
        self.body = source.pop()
        self.block = None
        if inst is not None:
            self.table = next((table for table in self.tables() if table.inst == inst), None)
            self.cell = next((cell for cell in self.table.cells if cell.address == address), self.table.ordered()[0]) if self.table else None
            if self.table is None:
                self.cell = None
        self.IsModified = True
        return True

    # Native edits.
    def native_calls(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == 'native']

    def native(self, name: str, items: dict[str, Any], *, sparse: bool) -> Any:
        where = [cell.address for cell in self._block_cells()] if self.block else self.cell.address if self.cell is not None else self.body_caret
        self.calls.append(('native', name, items, where))
        if self.execute_raises is not None:
            raise self.execute_raises
        if self.fail_action is not None and self.fail_action[0] == name:
            remaining = self.fail_action[1]
            if remaining == 0:
                return False
            self.fail_action = (name, remaining - 1)
        if self.execute_result is not True:
            return self.execute_result
        if name in {'TableDeleteRow', 'TableDeleteColumn'} and self.block:
            return False  # Measured: these two do nothing on a block.
        snapshot = copy.deepcopy(self.body)
        # The deep copy above keeps object identity out of the live document, so the live objects stay current.
        if name in {'CellBorder', 'CellZoneBorder'}:
            self._border(items)
        elif name == 'CellFill':
            if not sparse:
                self._fill(items)
        elif name == 'TableMergeCell':
            self._merge()
        elif name == 'TableInsertRowColumn':
            self._insert(items['Side'], items['Count'])
        elif name == 'TableDeleteRow':
            self._delete_lines('row', self.cell.row, 1)
        elif name == 'TableDeleteColumn':
            self._delete_lines('col', self.cell.col, 1)
        elif name == 'TableDeleteCell':
            cells = self._block_cells()
            if {cell.col for cell in cells} and min(c.col for c in cells) == 1 and max(c.col + c.cs for c in cells) - 1 == self.table.cols:
                self._delete_lines('row', min(c.row for c in cells), max(c.row + c.rs for c in cells) - min(c.row for c in cells))
            else:
                self._delete_lines('col', min(c.col for c in cells), max(c.col + c.cs for c in cells) - min(c.col for c in cells))
        elif name == 'TableSplitCell':
            self._split(items['Rows'], items['Cols'])
        elif name == 'TableCreate':
            self._create(items)
        else:
            raise AssertionError(f'unexpected native {name}')
        self.undo_stack.append(snapshot)
        self.redo_stack = []
        self.IsModified = True
        if self.corrupt_after_execute is not None:
            self.corrupt_after_execute(self)
        return True

    def TableSplitCell(self, Rows: int = 2, Cols: int = 0, DistributeHeight: int = 0, Merge: int = 0) -> Any:  # noqa: N802,N803
        return self.native('TableSplitCell', {'Rows': Rows, 'Cols': Cols}, sparse=False)

    def TableDeleteCell(self, remain_cell: bool = False) -> Any:  # noqa: N802
        return self.native('TableDeleteCell', {'remain_cell': remain_cell}, sparse=False)

    def create_table(self, rows: int = 1, cols: int = 1, treat_as_char: bool = True, width_type: int = 0, height_type: int = 0, header: bool = True, height: int = 0) -> Any:
        return self.native('TableCreate', {'Rows': rows, 'Cols': cols, 'header': header}, sparse=False)

    def CreateAction(self, name: str) -> _Action:  # noqa: N802
        return _Action(self, name)

    def _border(self, items: dict[str, Any]) -> None:
        cells = self._block_cells() if self.block else [self.cell]
        table = self.table
        top, bottom = min(c.row for c in cells), max(c.row + c.rs for c in cells) - 1
        left, right = min(c.col for c in cells), max(c.col + c.cs for c in cells) - 1
        names = {'left': 'Left', 'right': 'Right', 'top': 'Top', 'bottom': 'Bottom'}

        def line(old: tuple, kind_item: str, width_item: str, color_item: str) -> tuple:
            kind = items.get(kind_item)
            width = items.get(width_item)
            color = items.get(color_item)
            return (LINE_NAMES[kind] if kind is not None else old[0], WIDTH_NAMES[width] if width is not None else old[1], color if color is not None else old[2])

        def color_item(side: str) -> str:
            return 'BorderCorlorLeft' if side == 'left' else f'BorderColor{names[side]}'

        def on_side(cell: Cell, side: str) -> bool:
            return {'left': cell.col == left, 'right': cell.col + cell.cs - 1 == right, 'top': cell.row == top, 'bottom': cell.row + cell.rs - 1 == bottom}[side]

        for cell in cells:
            for side in SIDES:
                if on_side(cell, side):
                    cell.border[side] = line(cell.border[side], f'BorderType{names[side]}', f'BorderWidth{names[side]}', color_item(side))
                elif side in {'top', 'bottom'}:
                    cell.border[side] = line(cell.border[side], 'TypeHorz', 'WidthHorz', 'ColorHorz')
                else:
                    cell.border[side] = line(cell.border[side], 'TypeVert', 'WidthVert', 'ColorVert')
        if items.get('NoNeighborCell'):
            return
        for side in SIDES:
            if f'BorderType{names[side]}' not in items and f'BorderWidth{names[side]}' not in items:
                continue
            for other in table.cells:
                if other in cells:
                    continue
                facing = {'top': other.row + other.rs == top, 'bottom': other.row == bottom + 1, 'left': other.col + other.cs == left, 'right': other.col == right + 1}[side]
                if not facing:
                    continue
                inside = (other.col >= left and other.col + other.cs - 1 <= right) if side in {'top', 'bottom'} else (other.row >= top and other.row + other.rs - 1 <= bottom)
                if inside:
                    opposite = _OPPOSITE[side]
                    other.border[opposite] = line(other.border[opposite], f'BorderType{names[side]}', f'BorderWidth{names[side]}', color_item(side))

    def _fill(self, items: dict[str, Any]) -> None:
        for cell in self._block_cells() if self.block else [self.cell]:
            cell.fill = None if items['type'] == 0 else {'face': items['face'], 'hatch': items['hatch']}

    def _collapse(self, table: Table) -> None:
        for axis, span, attr in (('row', 'rs', 'rows'), ('col', 'cs', 'cols')):
            starts = sorted({getattr(cell, axis) for cell in table.cells})
            new_of = {line: number for number, line in enumerate(starts, start=1)}
            total = getattr(table, attr)
            for cell in table.cells:
                first, last = getattr(cell, axis), getattr(cell, axis) + getattr(cell, span)
                end = next((line for line in starts if line >= last), total + 1)
                setattr(cell, span, (new_of[end] if end in new_of else len(starts) + 1) - new_of[first])
                setattr(cell, axis, new_of[first])
            setattr(table, attr, len(starts))

    def _merge(self) -> None:
        cells = self._block_cells()
        table = self.table
        first = cells[0]
        kept = [item for cell in cells for item in (() if cell.blank() else cell.paragraphs)] or [para()]
        top, bottom = min(c.row for c in cells), max(c.row + c.rs for c in cells)
        left, right = min(c.col for c in cells), max(c.col + c.cs for c in cells)
        widths = {}
        for cell in table.cells:
            if cell.cs == 1:
                widths[cell.col] = cell.width
        corner = table.at(bottom - 1, right - 1)
        first.border = {'left': first.border['left'], 'top': first.border['top'], 'right': corner.border['right'], 'bottom': corner.border['bottom']}
        first.paragraphs, first.rs, first.cs = kept, bottom - top, right - left
        first.width = sum(widths.get(col, 0) for col in range(left, right)) or first.width
        table.cells = [cell for cell in table.cells if cell is first or cell not in cells]
        self._collapse(table)
        self.block = None
        self.cell = first

    def _insert(self, side: int, count: int) -> None:
        table, source = self.table, self.cell
        on_rows = side in {2, 3}
        axis, span, other, other_span = ('row', 'rs', 'col', 'cs') if on_rows else ('col', 'cs', 'row', 'rs')
        first = getattr(source, axis)
        at = first if side in {0, 2} else first + getattr(source, span)
        line = at if side in {0, 2} else at - 1
        mirrored = [cell for cell in table.cells if getattr(cell, axis) <= line < getattr(cell, axis) + getattr(cell, span)]
        for cell in table.cells:
            start = getattr(cell, axis)
            if start >= at:
                setattr(cell, axis, start + count)
            elif start + getattr(cell, span) > at:
                setattr(cell, span, getattr(cell, span) + count)
        outer = _OUTER_SIDE[side] if (at == 1 and side in {0, 2}) or (at == (table.rows if on_rows else table.cols) + 1 and side in {1, 3}) else None
        for number in range(count):
            for model in mirrored:
                start = getattr(model, axis)
                if start < at and start + getattr(model, span) - (count if start + getattr(model, span) - count > at else 0) > at:
                    continue
                if start < at < start + getattr(model, span):
                    continue
                new = Cell(1, 1, para(), width=model.width if on_rows else (model.width // model.cs), height=model.height)
                setattr(new, axis, at + number)
                setattr(new, other, getattr(model, other))
                setattr(new, span, 1)
                setattr(new, other_span, getattr(model, other_span))
                new.border, new.fill = dict(model.border), copy.deepcopy(model.fill)
                table.cells.append(new)
        if outer is not None:
            for model in mirrored:
                model.border[outer] = model.border[_OPPOSITE[outer]]
        if on_rows:
            table.rows += count
        else:
            table.cols += count

    def _delete_lines(self, axis: str, first: int, count: int) -> None:
        table = self.table
        span, total = ('rs', 'rows') if axis == 'row' else ('cs', 'cols')
        last = first + count
        lines = getattr(table, total)
        other = 'col' if axis == 'row' else 'row'
        if first == 1 or last == lines + 1:
            side = ('top' if axis == 'row' else 'left') if first == 1 else ('bottom' if axis == 'row' else 'right')
            outer_line = 1 if first == 1 else lines
            inner_line = last if first == 1 else first - 1
            for cell in table.cells:
                if getattr(cell, axis) <= inner_line < getattr(cell, axis) + getattr(cell, span):
                    removed = table.at(*((outer_line, cell.col) if axis == 'row' else (cell.row, outer_line)))
                    if removed is not None and not (getattr(removed, axis) <= inner_line < getattr(removed, axis) + getattr(removed, span)):
                        cell.border[side] = removed.border[side]
        kept: list[Cell] = []
        for cell in table.cells:
            start, end = getattr(cell, axis), getattr(cell, axis) + getattr(cell, span)
            if start >= first and end <= last:
                continue
            remaining = end - start - max(0, min(end, last) - max(start, first))
            setattr(cell, span, remaining)
            setattr(cell, axis, start if start < first else first if start < last else start - count)
            kept.append(cell)
        table.cells = kept
        setattr(table, total, getattr(table, total) - count)
        self.block = None
        self.cell = table.at(min(self.cell.row, table.rows), min(self.cell.col, table.cols)) if self.cell in kept else table.ordered()[0]
        self._collapse(table)

    def _col_edges(self, table: Table) -> list[int]:
        widths: dict[int, int] = {cell.col: cell.width for cell in table.cells if cell.cs == 1}
        edges = [0]
        for col in range(1, table.cols + 1):
            edges.append(edges[-1] + widths[col])
        return edges

    def _split(self, rows: int, cols: int) -> None:
        table, source = self.table, self.cell
        rows, cols = rows or 1, cols or 1
        edges = self._col_edges(table)
        x0, x1 = edges[source.col - 1], edges[source.col - 1 + source.cs]
        cuts = [x0 + (x1 - x0) * k // cols for k in range(cols + 1)]
        merged = sorted(set(edges) | set(cuts))
        for cell in table.cells:
            a, b = edges[cell.col - 1], edges[cell.col - 1 + cell.cs]
            cell.col, cell.cs = merged.index(a) + 1, merged.index(b) - merged.index(a)
        table.cols = len(merged) - 1
        added_rows = 0 if rows == source.rs else rows - 1
        top = source.row
        if added_rows:
            for cell in table.cells:
                if cell is source:
                    continue
                if cell.row > top:
                    cell.row += added_rows
                elif cell.row <= top < cell.row + cell.rs:
                    cell.rs += added_rows
            table.rows += added_rows
        pieces = []
        for r in range(rows):
            for c in range(cols):
                a, b = merged.index(cuts[c]), merged.index(cuts[c + 1])
                if r == 0 and c == 0:
                    source.col, source.cs, source.rs, source.width = a + 1, b - a, 1, cuts[1] - cuts[0]
                    continue
                piece = Cell(top + r, a + 1, para(), cs=b - a, width=cuts[c + 1] - cuts[c], height=source.height)
                piece.border, piece.fill = dict(source.border), copy.deepcopy(source.fill)
                if c:
                    piece.border['left'] = source.border['right']
                if r:
                    piece.border['top'] = source.border['bottom']
                pieces.append(piece)
        table.cells.extend(pieces)

    def _create(self, items: dict[str, Any]) -> None:
        number = len(self.tables()) + 500
        cols = items['Cols']
        table = Table(str(number), items['Rows'], cols, [Cell(r, c, para(), width=41954 // cols) for r in range(1, items['Rows'] + 1) for c in range(1, cols + 1)])
        at = self.body_caret[1] + 1
        self.body[at:at] = [table, '']
        self.table, self.cell = table, table.ordered()[0]

    # HWPML2X.
    def _fills(self) -> list[tuple]:
        fills = [(SOLID, SOLID, SOLID, SOLID, None)]

        for table in self.tables():
            for cell in table.cells:
                key = (*[cell.border[side] for side in SIDES], self._fill_key(cell.fill))
                if key not in fills:
                    fills.append(key)
        return fills

    @staticmethod
    def _fill_key(fill: Any) -> Any:
        return tuple(sorted(fill.items())) if isinstance(fill, dict) else fill

    def _fill_xml(self, number: int, key: tuple) -> str:
        sides = ''
        for side, line in zip(('LEFT', 'RIGHT', 'TOP', 'BOTTOM'), key[:4]):
            color = f' Color="{line[2]}"' if line[2] else ''
            sides += f'<{side}BORDER{color} Type="{line[0]}" Width="{line[1]}"/>'
        fill = ''
        if key[4]:
            values = dict(key[4])
            if values.get('kind') == 'gradation':
                fill = '<FILLBRUSH><GRADATION Angle="0" Type="Linear"/></FILLBRUSH>'
            else:
                fill = f'<FILLBRUSH><WINDOWBRUSH Alpha="0" FaceColor="{values["face"]}" HatchColor="{values["hatch"]}"/></FILLBRUSH>'
        return (f'<BORDERFILL BackSlash="0" BreakCellSeparateLine="0" CenterLine="0" CounterBackSlash="0" CounterSlash="0" CrookedSlash="0" Id="{number}" Shadow="false" Slash="0" ThreeD="false">'
                f'{sides}<DIAGONAL Type="Solid" Width="0.1mm"/>{fill}</BORDERFILL>')

    def _para_xml(self, item: dict[str, Any], fills: list[tuple], *, head: str = '') -> str:
        out = ['<P ColumnBreak="false" PageBreak="false" ParaShape="0" Style="0">']
        runs = item['runs']
        if not runs and not head:
            out.append('<TEXT CharShape="0"/>')
        if head:
            out.append(f'<TEXT CharShape="0">{head}</TEXT>')
        for run in runs:
            if run == 'picture':
                out.append(f'<TEXT CharShape="0">{_PICTURE}</TEXT>')
            elif isinstance(run, Table):
                out.append(f'<TEXT CharShape="0">{self._table_xml(run, fills)}<CHAR/></TEXT>')
            else:
                text, shape = run
                inner = ''
                for piece in re.split('(\t)', text):
                    inner += '<TAB/>' if piece == '\t' else escape(piece)
                out.append(f'<TEXT CharShape="{shape}">' + (f'<CHAR>{inner}</CHAR>' if text else '') + '</TEXT>')
        out.append('</P>')
        return ''.join(out)

    def _table_xml(self, table: Table, fills: list[tuple]) -> str:
        rows = []
        for row in range(1, table.rows + 1):
            cells = ''
            for cell in [item for item in table.ordered() if item.row == row]:
                key = (*[cell.border[side] for side in SIDES], self._fill_key(cell.fill))
                body = ''.join(self._para_xml(item, fills) for item in cell.paragraphs)
                cells += (f'<CELL BorderFill="{fills.index(key) + 1}" ColAddr="{cell.col - 1}" ColSpan="{cell.cs}" Dirty="false" Editable="false" HasMargin="false" Header="false" Height="{cell.height}" Protect="false" '
                          f'RowAddr="{cell.row - 1}" RowSpan="{cell.rs}" Width="{cell.width}"><CELLMARGIN Bottom="141" Left="510" Right="510" Top="141"/><PARALIST LineWrap="Break" LinkListID="0" LinkListIDNext="0" TextDirection="0" VertAlign="Center">{body}</PARALIST></CELL>')
            rows.append(f'<ROW>{cells}</ROW>')
        return (f'<TABLE BorderFill="1" CellSpacing="0" ColCount="{table.cols}" PageBreak="Cell" RepeatHeader="true" RowCount="{table.rows}"><SHAPEOBJECT InstId="{table.inst}" Lock="false" NumberingType="Table" TextWrap="TopAndBottom" ZOrder="1">'
                '<SIZE Height="9000" HeightRelTo="Absolute" Protect="false" Width="36000" WidthRelTo="Absolute"/><POSITION AffectLSpacing="false" AllowOverlap="false" FlowWithText="true" HoldAnchorAndSO="false" HorzAlign="Left" HorzOffset="0" HorzRelTo="Column" TreatAsChar="true" VertAlign="Top" VertOffset="0" VertRelTo="Para"/>'
                '<OUTSIDEMARGIN Bottom="283" Left="283" Right="283" Top="283"/></SHAPEOBJECT><INSIDEMARGIN Bottom="141" Left="510" Right="510" Top="141"/>' + ''.join(rows) + '</TABLE>')

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        self.calls.append(('get_text_file', fmt, option))
        if fmt != 'HWPML2X':
            return ''
        fills = self._fills()
        parts = []
        for number, item in enumerate(self.body):
            head = _SECTION_HEAD if number == 0 else ''
            if isinstance(item, Table):
                parts.append(self._para_xml({'runs': [item]}, fills, head=head))
            elif isinstance(item, str):
                parts.append(self._para_xml(para(item) if item else {'runs': []}, fills, head=head))
            else:
                parts.append(self._para_xml(item, fills, head=head))
        head_xml = _CAPTURED[:_BODY_START]
        listed = f'<BORDERFILLLIST Count="{len(fills)}">' + ''.join(self._fill_xml(number + 1, key) for number, key in enumerate(fills)) + '</BORDERFILLLIST>'
        head_xml = re.sub(r'<BORDERFILLLIST .*?</BORDERFILLLIST>', lambda _m: listed, head_xml, flags=re.S)
        head_xml = re.sub(r'BorderFillId="\d+"', 'BorderFillId="1"', head_xml)
        head_xml = re.sub(r'BorderFill="\d+"', 'BorderFill="1"', head_xml)
        return head_xml + f'<BODY><SECTION Id="0">{"".join(parts)}</SECTION>' + _CAPTURED[_BODY_END:]


def sample_document() -> TableHwp:
    """Two similar tables, an empty one, repeated rows, merged cells, and text around them."""
    first_rows = [[f'{"ABCD"[c]}{r + 1}' for c in range(4)] for r in range(5)]
    first_rows[1][0] = {'paragraphs': [para('가나 ABC  두칸')]}
    first_rows[2][1] = {'paragraphs': [para('B3앞\tB3뒤')]}
    first_rows[2][2] = {'paragraphs': [para(('C3굵게', 1), ('C3보통', 0))]}
    first_rows[3][3] = {'paragraphs': [para('D4첫문단'), para('D4둘째문단')]}
    first_rows[4][0] = {'paragraphs': [para('picture', 'A5그림뒤')]}
    return TableHwp([
        '본문 첫 문단',
        '표일 앞 문단',
        grid('1011', first_rows),
        '표이 앞 문단',
        grid('1012', [[f'{"ABCD"[c]}{r + 1}' for c in range(4)] for r in range(5)]),
        grid('1013', [['', '', ''] for _ in range(3)]),
        grid('1014', [['같은글', '같은글'], ['같은글', '같은글'], ['같은글', '같은글'], ['끝왼쪽', '끝오른쪽']]),
        grid('1015', [[{'paragraphs': [para('병합머리')], 'cs': 2}, None, '머리오른쪽'],
                      [{'paragraphs': [para('세로병합')], 'rs': 2}, '둘째가운데', {'paragraphs': [para('채움셀')], 'fill': {'face': 0xCCF2FF, 'hatch': 0x999999}}],
                      [None, '셋째가운데', '셋째오른쪽']]),
        grid('1016', [['칠A1', '칠B1', '칠C1'], ['칠A2', '칠B2', '칠C2']], widths=[6000, 12000, 18000]),
        grid('1017', [['구갑', '', '구병'], ['', '구정', {'paragraphs': [para(), para('구무둘째')]}]]),
        '맺음 문단',
    ], pages={'1016': 2, '1017': 2})
