"""A numbering bundle that leaves the session held must refuse plain undo, even with an older counted edit.

Integrating the table-structure undo accounting into command_bundle kept the numbering held-custody branch; that branch
now clears the undo count and records why. Uses the real runtime manager, command_bundle and _execute_live from the
custody harness with a faithful fake SDK. This is not Hancom evidence.
"""
from __future__ import annotations

import unittest

from app.local_cli_runtime import LocalCliRuntimeError
from tests.test_native_numbering_bundle_translation import NUMBERING_STEP
from tests.test_native_numbering_runtime_custody import CustodyAssertions, Owned, make_hwp


class HeldNumberingBundleUndoTests(CustodyAssertions, unittest.TestCase):
    def test_a_held_numbering_bundle_refuses_plain_undo_over_an_older_count(self) -> None:
        hwp = make_hwp()
        owned = Owned(self, hwp, real_execute_live=True)
        owned.arm('ParagraphShape', make_exc=KeyboardInterrupt)
        base = {'session_id': owned.sid, 'command_generation': 0, 'native_command_sequence': 0, 'pending_logical_undo_count': 1}
        owned.service._load_active_binding = lambda session_id=None: dict(base)
        owned.service._read_binding = lambda session_id=None: dict(base)
        owned.run(NUMBERING_STEP)
        last = owned.saved_bindings[-1]
        self.assertTrue(last.get('native_custody_held'))
        self.assertIsNone(last.get('pending_logical_undo_count'))
        self.assertIn('unknown native outcome', str(last.get('logical_undo_unverified')))
        owned.service._load_active_binding = lambda session_id=None: dict(last)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            owned.service.undo()


if __name__ == '__main__':
    unittest.main()
