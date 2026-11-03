"""Navigation-only live commands and close debt, through the real owning STA.

move, select, cell, cellmove and cursormove only find, set the caret, select and move (TableCellBlock,
Table*Cell, Move*), so with no earlier debt or hold and IsModified exactly False before and after they add no
close debt, like a clean export. Earlier edit debt is never cleared, a modified document keeps the debt, a
failing IsModified getter is not False, and other commands are unchanged.
"""
from __future__ import annotations

import unittest

from tests.test_repair_runtime_safety import owned_doc

NAVIGATION = ('move', 'select', 'cell', 'cellmove', 'cursormove')


def execute(owned, name, handler=None):
    return owned.manager.execute(session_id=owned.sid, command_name=name, handler=handler or (lambda handle: {'ok': True}), timeout=5)


class NavigationCloseDebt(unittest.TestCase):
    def test_clean_navigation_adds_no_close_debt(self):
        for name in NAVIGATION:
            with self.subTest(name=name):
                owned = owned_doc(self)
                execute(owned, name)
                self.assertFalse(owned.session._normal_close_requires_save)
                self.assertFalse(owned.session._native_custody_held)

    def test_modified_before_or_during_keeps_the_debt(self):
        for name in NAVIGATION:
            for when in ('before', 'during'):
                with self.subTest(name=name, when=when):
                    owned = owned_doc(self)
                    if when == 'before':
                        owned.hwp.IsModified = True
                        execute(owned, name)
                    else:
                        def handler(handle):
                            handle.hwp.IsModified = True
                            return {'ok': True}
                        execute(owned, name, handler)
                    self.assertTrue(owned.session._normal_close_requires_save)

    def test_earlier_edit_debt_is_not_cleared(self):
        for name in NAVIGATION:
            with self.subTest(name=name):
                owned = owned_doc(self)
                execute(owned, 'type')
                self.assertTrue(owned.session._normal_close_requires_save)
                execute(owned, name)
                self.assertTrue(owned.session._normal_close_requires_save)

    def test_failing_is_modified_getter_is_not_false(self):
        for name in NAVIGATION:
            with self.subTest(name=name):
                owned = owned_doc(self)
                hwp = owned.hwp

                class Unreadable(type(hwp)):
                    @property
                    def IsModified(self):  # noqa: N802
                        raise OSError('synthetic IsModified failure')
                hwp.__class__ = Unreadable
                with self.assertRaises(Exception):
                    execute(owned, name)
                self.assertTrue(owned.session._native_custody_held)

    def test_other_live_commands_keep_their_debt(self):
        for name in ('type', 'bold', 'action', 'pycall', 'figure-section'):
            with self.subTest(name=name):
                owned = owned_doc(self)
                execute(owned, name)
                self.assertTrue(owned.session._normal_close_requires_save)


if __name__ == '__main__':
    unittest.main()
