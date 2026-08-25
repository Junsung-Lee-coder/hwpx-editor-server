from __future__ import annotations

import unittest
from typing import Any

from app.edit_ops import EditOperationError, _resolve_current_table_cell_for_replacement, apply_edit_operations


class FakeHAction:
    def __init__(self, hwp: 'FakeHwp') -> None:
        self.hwp = hwp

    def Run(self, action: str) -> bool:
        if action in {'Delete', 'Erase'}:
            self.hwp.delete_selection()
            return True
        if action == 'TableCellBlock':
            self.hwp.TableCellBlock()
            return True
        if action == 'TableRightCell':
            self.hwp.TableRightCell()
            return True
        if action == 'TableLeftCell':
            self.hwp.TableLeftCell()
            return True
        if action == 'TableLowerCell':
            self.hwp.TableLowerCell()
            return True
        if action == 'TableUpperCell':
            self.hwp.TableUpperCell()
            return True
        if action == 'PutParaNumber':
            self.hwp.PutParaNumber()
            return True
        if action == 'PutBullet':
            self.hwp.PutBullet()
            return True
        return True


class FakeHwp:
    def __init__(self) -> None:
        self.tables: list[dict[str, str]] = []
        self.paragraphs: list[str] = []
        self.paragraph_list_meta: dict[int, dict[str, Any]] = {}
        self.mode = 'paragraph'
        self.current_table = 0
        self.current_addr = 'A1'
        self.para = 0
        self.pos = 0
        self.selection: tuple[Any, ...] | None = None
        self.selection_kind: str | None = None
        self.selection_mode: int | None = None
        self.find_cursor = 0
        self.put_field_text_calls: list[tuple[str, str]] = []
        self.move_to_field_calls: list[tuple[str, bool]] = []
        self.global_field_list_even_for_current_table = False
        self.global_move_to_field_first_match = False
        self.HAction = FakeHAction(self)

    def _addr_to_row_col(self, addr: str) -> tuple[int, int]:
        col_letters = ''.join(ch for ch in addr if ch.isalpha())
        row_digits = ''.join(ch for ch in addr if ch.isdigit())
        col = 0
        for ch in col_letters:
            col = col * 26 + (ord(ch) - ord('A') + 1)
        return int(row_digits), col

    def _row_col_to_addr(self, row: int, col: int) -> str:
        letters = ''
        n = col
        while n:
            n, rem = divmod(n - 1, 26)
            letters = chr(ord('A') + rem) + letters
        return f'{letters}{row}'

    def MoveDocBegin(self) -> None:
        self.find_cursor = 0
        if self.tables:
            self.mode = 'table'
            self.current_table = 0
            self.current_addr = sorted(self.tables[0], key=lambda a: self._addr_to_row_col(a))[0]
        elif self.paragraphs:
            self.mode = 'paragraph'
            self.para = 0
            self.pos = 0
        self.selection = None
        self.selection_kind = None

    def get_pos(self) -> tuple[int, int, int]:
        if self.mode == 'table':
            row, col = self._addr_to_row_col(self.current_addr)
            return (self.current_table, row - 1, col - 1)
        return (0, self.para, self.pos)

    def set_pos(self, list_id: int, para: int, pos: int) -> None:
        self.selection = None
        self.selection_kind = None
        if self.tables and 0 <= list_id < len(self.tables):
            addr = self._row_col_to_addr(para + 1, pos + 1)
            if addr in self.tables[list_id]:
                self.mode = 'table'
                self.current_table = list_id
                self.current_addr = addr
                return
        self.mode = 'paragraph'
        self.para = max(0, min(int(para), max(0, len(self.paragraphs) - 1)))
        self.pos = max(0, int(pos))

    def is_cell(self) -> bool:
        return self.mode == 'table'

    def get_cell_addr(self) -> str | None:
        return self.current_addr if self.mode == 'table' else None

    def SelectionMode(self) -> int | None:
        return self.selection_mode

    def get_selected_pos(self) -> tuple[Any, ...]:
        return self.selection or (False, 0, 0, 0, 0, 0, 0)

    def get_selected_text(self, keep_select: bool = True) -> str:
        if self.selection_kind == 'cell':
            return self.tables[self.current_table].get(self.current_addr, '')
        if self.selection_kind == 'paragraph' and self.selection:
            _, _list_id, start_para, _start_pos, _end_list, end_para, _end_pos = self.selection
            return '\n'.join(self.paragraphs[start_para : end_para + 1])
        return ''

    def select_text(self, *args: Any) -> None:
        if len(args) == 1 and isinstance(args[0], tuple):
            selected = args[0]
            self.selection = selected
            if self.mode == 'table':
                self.selection_kind = 'cell'
            else:
                self.selection_kind = 'paragraph'
            self.selection_mode = 3
            return
        if len(args) == 5:
            start_para, start_pos, end_para, end_pos, list_id = args
            self.mode = 'paragraph'
            self.para = int(start_para)
            self.pos = int(start_pos)
            self.selection = (True, int(list_id), int(start_para), int(start_pos), int(list_id), int(end_para), int(end_pos))
            self.selection_kind = 'paragraph'
            self.selection_mode = 0
            return
        raise TypeError(args)

    def find(self, target: str, **_kwargs: Any) -> bool:
        flat: list[tuple[str, int, str | int, str]] = []
        for table_index, table in enumerate(self.tables):
            for addr in sorted(table, key=lambda a: self._addr_to_row_col(a)):
                flat.append(('table', table_index, addr, table[addr]))
        for para_index, text in enumerate(self.paragraphs):
            flat.append(('paragraph', 0, para_index, text))
        for index in range(self.find_cursor, len(flat)):
            kind, table_index, addr_or_para, text = flat[index]
            if target in text:
                self.find_cursor = index + 1
                if kind == 'table':
                    self.mode = 'table'
                    self.current_table = int(table_index)
                    self.current_addr = str(addr_or_para)
                    row, col = self._addr_to_row_col(self.current_addr)
                    self.selection = (True, self.current_table, row - 1, col - 1, self.current_table, row - 1, col)
                    self.selection_kind = 'cell'
                    self.selection_mode = 3
                else:
                    self.mode = 'paragraph'
                    self.para = int(addr_or_para)
                    self.pos = 0
                    self.selection = (True, 0, self.para, 0, 0, self.para, -1)
                    self.selection_kind = 'paragraph'
                    self.selection_mode = 0
                return True
        return False

    def fill_addr_field(self) -> None:
        return None

    def get_field_list(self, *args: Any) -> str:
        if args == (1, 1) and self.mode == 'table' and not self.global_field_list_even_for_current_table:
            return '\x02'.join(sorted(self.tables[self.current_table], key=lambda a: self._addr_to_row_col(a)))
        parts: list[str] = []
        counts: dict[str, int] = {}
        for table in self.tables:
            for addr in sorted(table, key=lambda a: self._addr_to_row_col(a)):
                suffix = counts.get(addr, 0)
                parts.append(f'{addr}{{{{{suffix}}}}}')
                counts[addr] = suffix + 1
        return '\x02'.join(parts)

    def move_to_field(self, field: str, **_kwargs: Any) -> bool:
        self.move_to_field_calls.append((field, bool(_kwargs.get('select', False))))
        if not self.global_move_to_field_first_match and self.mode == 'table' and field in self.tables[self.current_table]:
            self.current_addr = field
            self.selection = None
            self.selection_kind = None
            return True
        for index, table in enumerate(self.tables):
            if field in table:
                self.mode = 'table'
                self.current_table = index
                self.current_addr = field
                self.selection = None
                self.selection_kind = None
                return True
        return False

    def TableCellBlock(self) -> bool:
        if self.mode != 'table':
            return False
        row, col = self._addr_to_row_col(self.current_addr)
        self.selection = (True, self.current_table, row - 1, col - 1, self.current_table, row - 1, col)
        self.selection_kind = 'cell'
        self.selection_mode = 3
        return True

    def _move_cell(self, d_row: int, d_col: int) -> bool:
        row, col = self._addr_to_row_col(self.current_addr)
        target = self._row_col_to_addr(row + d_row, col + d_col)
        if target in self.tables[self.current_table]:
            self.current_addr = target
        self.selection = None
        self.selection_kind = None
        return True

    def TableRightCell(self) -> bool:
        return self._move_cell(0, 1)

    def TableLeftCell(self) -> bool:
        return self._move_cell(0, -1)

    def TableLowerCell(self) -> bool:
        return self._move_cell(1, 0)

    def TableUpperCell(self) -> bool:
        return self._move_cell(-1, 0)

    def put_field_text(self, field: str, text: str) -> None:
        self.put_field_text_calls.append((field, text))
        for table in self.tables:
            if field in table:
                table[field] = text
                return

    def delete_selection(self) -> None:
        if self.selection_kind == 'cell':
            self.tables[self.current_table][self.current_addr] = ''
        elif self.selection_kind == 'paragraph' and self.selection:
            _, _list_id, start_para, _start_pos, _end_list, end_para, _end_pos = self.selection
            del self.paragraphs[start_para : end_para + 1]
            self.para = start_para
            self.pos = 0
        self.selection = None
        self.selection_kind = None

    def insert_text(self, text: str) -> None:
        if self.mode == 'table':
            self.tables[self.current_table][self.current_addr] = text
            self.selection = None
            self.selection_kind = None
            return
        lines = text.split('\r\n')
        if len(lines) == 1:
            self.paragraphs.insert(self.para, text)
        else:
            for offset, line in enumerate(lines):
                self.paragraphs.insert(self.para + offset, line)
            self.para = self.para + len(lines) - 1
        self.selection = None
        self.selection_kind = None

    def PutParaNumber(self) -> None:
        self.paragraph_list_meta[self.para] = {'kind': 'number', 'level': 1}

    def PutBullet(self) -> None:
        self.paragraph_list_meta[self.para] = {'kind': 'bullet', 'level': 1}

    def get_parashape(self) -> dict[str, Any]:
        return dict(self.paragraph_list_meta.get(self.para, {'HeadingLevel': 1}))

    def set_parashape(self, shape: dict[str, Any]) -> None:
        if self.mode == 'paragraph':
            meta = self.paragraph_list_meta.setdefault(self.para, {})
            if 'HeadingLevel' in shape:
                meta['level'] = shape['HeadingLevel']
            if 'Level' in shape:
                meta['level'] = shape['Level']


class TableScopedCellPatchTests(unittest.TestCase):
    def test_current_table_cell_resolver_never_uses_global_field_move(self) -> None:
        hwp = FakeHwp()
        hwp.tables = [
            {'A1': 'Item', 'B1': 'Content', 'A2': 'Overview marker', 'B2': 'OLD_OVERVIEW'},
            {'A1': 'Category', 'B1': 'Strategy', 'A2': 'Market marker', 'B2': 'OLD_MARKET'},
        ]
        hwp.mode = 'table'
        hwp.current_table = 1
        hwp.current_addr = 'A2'

        result = _resolve_current_table_cell_for_replacement(hwp, 'B2')

        self.assertEqual(hwp.current_table, 1)
        self.assertEqual(hwp.current_addr, 'B2')
        self.assertEqual(hwp.tables[0]['B2'], 'OLD_OVERVIEW')
        self.assertEqual(hwp.move_to_field_calls, [])
        self.assertEqual(result['match_strategy'], 'current_table_scoped_cell_navigation')
        self.assertEqual(result['target_snapshot']['cell_addr'], 'B2')
        self.assertEqual(result['global_field_write_preflight']['decision'], 'rejected_for_current_table_cell_replace')

    def test_current_table_cell_resolver_fails_closed_outside_table(self) -> None:
        hwp = FakeHwp()
        hwp.tables = [{'A1': 'Item', 'B1': 'Content', 'A2': 'Overview marker', 'B2': 'OLD_OVERVIEW'}]
        hwp.paragraphs = ['outside table']
        hwp.mode = 'paragraph'
        hwp.para = 0

        with self.assertRaisesRegex(EditOperationError, 'requires the cursor to already be inside the intended table'):
            _resolve_current_table_cell_for_replacement(hwp, 'B2')
        self.assertEqual(hwp.move_to_field_calls, [])

    def test_table_patch_cells_uses_scoped_navigation_not_global_putfieldtext(self) -> None:
        hwp = FakeHwp()
        hwp.tables = [
            {'A1': 'Item', 'B1': 'Content', 'A2': 'Overview marker', 'B2': 'OLD_OVERVIEW'},
            {'A1': 'Category', 'B1': 'Strategy', 'A2': 'Market marker', 'B2': 'OLD_MARKET'},
        ]

        summary = apply_edit_operations(
            hwp,
            [
                {
                    'op': 'table_patch_cells',
                    'entry_find': 'Market marker',
                    'expected_entry_cell_addr': 'A2',
                    'patches': [{'cell_addr': 'B2', 'replace': 'NEW_MARKET\nLINE2'}],
                }
            ],
        )

        self.assertEqual(hwp.tables[0]['B2'], 'OLD_OVERVIEW')
        self.assertEqual(hwp.tables[1]['B2'], 'NEW_MARKET\nLINE2')
        self.assertEqual(hwp.put_field_text_calls, [])
        transition = summary[0]['transitions'][0]
        self.assertEqual(transition['patch_results'][0]['replace_strategy'], 'scoped_cell_navigation_clear_insert_readback')
        self.assertFalse(transition['global_field_write_preflight']['safe_for_global_field_write'])
        self.assertIn('B2', transition['global_field_write_preflight']['duplicate_targets'])

    def test_table_patch_cells_ignores_global_field_list_cursor_taint_before_scoped_writes(self) -> None:
        hwp = FakeHwp()
        hwp.tables = [
            {'A1': 'Schedule item', 'B1': 'Schedule period', 'A2': 'Schedule 1', 'B2': 'OLD_SCHEDULE'},
            {'A1': 'Item', 'B1': 'Content', 'A2': 'Overview marker', 'B2': 'OLD_OVERVIEW'},
        ]
        hwp.global_field_list_even_for_current_table = True
        hwp.global_move_to_field_first_match = True

        summary = apply_edit_operations(
            hwp,
            [
                {
                    'op': 'table_patch_cells',
                    'entry_find': 'Overview marker',
                    'expected_entry_cell_addr': 'A2',
                    'patches': [{'cell_addr': 'B2', 'replace': 'NEW_OVERVIEW'}],
                }
            ],
        )

        self.assertEqual(hwp.tables[0]['B2'], 'OLD_SCHEDULE')
        self.assertEqual(hwp.tables[1]['B2'], 'NEW_OVERVIEW')
        self.assertEqual(hwp.put_field_text_calls, [])
        self.assertEqual(hwp.move_to_field_calls, [])
        transition = summary[0]['transitions'][0]
        self.assertEqual(transition['live_table_fingerprint']['skipped'], 'not_required_without_expected_table_fingerprint')
        self.assertEqual(transition['patch_results'][0]['before']['cell_addr'], 'B2')
        self.assertEqual(transition['patch_results'][0]['readback']['selected_text'], 'NEW_OVERVIEW')

    def test_merged_or_unreachable_cell_fails_closed_with_navigation_diagnostic(self) -> None:
        hwp = FakeHwp()
        hwp.tables = [{'A1': 'Item', 'B1': 'Content', 'A2': 'Example material cost', 'B2': 'OLD'}]

        with self.assertRaisesRegex(EditOperationError, 'table-scoped cell navigation'):
            apply_edit_operations(
                hwp,
                [
                    {
                        'op': 'table_patch_cells',
                        'entry_find': 'Example material cost',
                        'expected_entry_cell_addr': 'A2',
                        'patches': [{'cell_addr': 'A7', 'replace': 'Total'}],
                    }
                ],
            )


class EmptyNativeListScaffoldTests(unittest.TestCase):
    def test_replace_empty_native_list_scaffold_keeps_paragraphs_and_native_levels(self) -> None:
        hwp = FakeHwp()
        hwp.paragraphs = ['2. Background', '', 'Next section']
        hwp.mode = 'paragraph'
        hwp.para = 1

        summary = apply_edit_operations(
            hwp,
            [
                {
                    'op': 'replace_empty_native_list_scaffold',
                    'cursor_pos': [0, 1, 0],
                    'kind': 'number',
                    'require_empty_scaffold': True,
                    'items': [
                        {'text': 'Environment requirement', 'kind': 'number', 'level': 1},
                        {'text': 'Market validation', 'kind': 'number', 'level': 1},
                    ],
                    'forbidden_concatenations': ['2. BackgroundEnvironment requirement'],
                }
            ],
        )

        self.assertEqual(hwp.paragraphs, ['2. Background', 'Environment requirement', 'Market validation', 'Next section'])
        self.assertEqual(hwp.paragraph_list_meta[1]['kind'], 'number')
        self.assertEqual(hwp.paragraph_list_meta[2]['kind'], 'number')
        self.assertNotIn('2. BackgroundEnvironment requirement', '\n'.join(hwp.paragraphs))
        self.assertEqual(summary[0]['matches'], 1)
        self.assertEqual(len(summary[0]['transitions'][0]['item_proofs']), 2)

    def test_native_list_scaffold_rejects_typed_markers(self) -> None:
        hwp = FakeHwp()
        hwp.paragraphs = ['']
        hwp.mode = 'paragraph'
        hwp.para = 0

        with self.assertRaisesRegex(EditOperationError, 'omit typed bullet'):
            apply_edit_operations(
                hwp,
                [
                    {
                        'op': 'replace_empty_native_list_scaffold',
                        'cursor_pos': [0, 0, 0],
                        'kind': 'number',
                        'items': [{'text': '1) typed marker', 'kind': 'number', 'level': 1}],
                    }
                ],
            )


if __name__ == '__main__':
    unittest.main()
