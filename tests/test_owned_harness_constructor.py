"""The real-manager test harness and the production constructor guard.

The harness drives the real manager and owning thread over a fake SDK. Construction is not its subject: it takes the path of a runtime
with no native process baseline, which is what every portable run takes, and on Windows too. The guard itself stays as it is, and with a
process baseline present it still refuses this harness's fake constructor, here and in tests/test_cli_native_close.py.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app import local_cli_runtime as rt
from tests.native_table_fakes import sample_document
from tests.test_native_numbering_runtime_custody import Owned


class Harness(unittest.TestCase):
    def test_the_harness_takes_the_no_baseline_path_on_every_platform(self) -> None:
        with patch.object(rt.os.sys, 'platform', 'win32'), patch.object(rt.subprocess, 'run', side_effect=AssertionError('the harness never lists native processes')):
            owned = Owned(self, sample_document())
        self.assertTrue(owned.session._thread.is_alive())
        self.assertIsNone(getattr(owned.session, '_native_close_owner', None))

    def test_with_a_process_baseline_the_production_guard_still_refuses_the_fake_constructor(self) -> None:
        with patch.object(Owned, 'native_baseline', staticmethod(lambda: frozenset())):
            with self.assertRaises(rt.LocalCliRuntimeError) as caught:
                Owned(self, sample_document())
        self.assertIn('Native constructor wrapper contract is unproved', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
