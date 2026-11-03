"""Original errors survive, and no product module references an undefined global.

- replace_paragraph_range_safe: when selecting the range's end paragraph fails, the job error is that
  EditOperationError, not a NameError from a branch that referenced an undefined flag.
- A symtable check over the product packages: every implicit global used in a function or at module level
  is defined in that module (assignment, import, def/class) or is a builtin.
"""
from __future__ import annotations

import builtins
import pathlib
import symtable
import unittest
from unittest import mock

from app import edit_ops

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = ('app', 'local_cli_v1', 'hwpx_mcp')


class _FindingHwp:
    """Every find() hits; selections and caret moves are recorded only."""

    def __init__(self):
        self.calls = []

    def find(self, text, **kwargs):
        self.calls.append(('find', text))
        return True


class RangeEndSelectionFailure(unittest.TestCase):
    def test_end_paragraph_selection_error_is_the_job_error(self):
        hwp = _FindingHwp()
        start = (True, 0, 1, 0, 0, 1, 5)
        failure = edit_ops.EditOperationError('end paragraph could not be selected')
        with mock.patch.object(edit_ops, '_move_doc_begin'), \
                mock.patch.object(edit_ops, '_guard_against_packed_paragraph'), \
                mock.patch.object(edit_ops, '_set_pos'), \
                mock.patch.object(edit_ops, '_select_whole_paragraph_for_current_selection', side_effect=[start, failure]):
            with self.assertRaises(edit_ops.EditOperationError) as caught:
                edit_ops._safe_replace_paragraph_range(hwp, '시작', '끝', '대체', apply='first', match_case=True, whole_word=False)
        self.assertIs(caught.exception, failure)
        self.assertEqual([c for c in hwp.calls if c[0] == 'find'], [('find', '시작'), ('find', '끝')])


def _module_defined(table: symtable.SymbolTable) -> set[str]:
    return {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}


def _undefined(path: pathlib.Path) -> list[tuple[str, str]]:
    source = path.read_text(encoding='utf-8')
    top = symtable.symtable(source, str(path), 'exec')
    defined = _module_defined(top) | set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__spec__', '__path__',
                                                           '__package__', '__builtins__', '__loader__', '__annotations__'}
    if any(s.is_imported() and s.get_name() == '*' for s in top.get_symbols()):
        return []
    missing = []

    def visit(table, scope):
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if not symbol.is_referenced():
                continue
            module_level = table.get_type() == 'module'
            implicit_global = symbol.is_global() and not symbol.is_declared_global() if not module_level else not (symbol.is_assigned() or symbol.is_imported())
            if implicit_global and name not in defined:
                missing.append((scope, name))
        for child in table.get_children():
            visit(child, f'{scope}.{child.get_name()}')
    visit(top, path.name)
    return missing


class NoUndefinedGlobals(unittest.TestCase):
    def test_product_modules_define_every_global_they_use(self):
        found = []
        for package in PACKAGES:
            for path in sorted((ROOT / package).rglob('*.py')):
                found += [(str(path.relative_to(ROOT)), scope, name) for scope, name in _undefined(path)]
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
