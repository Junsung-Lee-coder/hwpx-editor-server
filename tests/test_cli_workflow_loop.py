"""Actual CLI help contract, no backend transport; normal close is part of the loop."""
import contextlib
import io
import unittest
from local_cli_v1.main import main


class WorkflowLoopTests(unittest.TestCase):
    def test_public_help_contains_functional_close_loop_and_explicit_held_limit(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['help', 'workflow']), 0)
        text = output.getvalue()
        self.assertIn('status -> open -> find/where/select -> edit -> screenshot/export-proof -> save -> close', text)
        self.assertIn('clean no-op', text)
        self.assertIn('UNKNOWN', text)
        self.assertIn('download', text)


if __name__ == '__main__':
    unittest.main()
