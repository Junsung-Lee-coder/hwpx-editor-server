"""Supplemental current-source public projections and post-edit stopping paths."""
from __future__ import annotations
import inspect
import unittest
from app import local_cli_runtime as rt
from app.local_cli_service import LocalCliService
from tests.native_table_fakes import sample_document
from tests.test_guarded_bundle_observations import real_owned, run
from tests.test_native_table import binding


class PostEdit(unittest.TestCase):
    def test_final_explicit_observation_failure_retains_applied_step_and_primary_cause(self):
        for method in ('get_pos', 'get_selected_pos'):
            for malformed in (False, True):
                with self.subTest(method=method, malformed=malformed):
                    hwp = sample_document()
                    step = {'op': 'table_style_exact', **binding(hwp, '1012'), 'scope': 'cell', 'cell': 'B2', 'fill': '#FF0000'}
                    owner = real_owned(self, hwp)
                    fired = []
                    after = []
                    cause = OSError('synthetic final explicit observation')
                    # Instrument every fake SDK method; only the actual where
                    # package read can fire, after the table package returned.
                    for name in ('get_pos', 'get_selected_pos', 'get_text_file', 'set_pos', 'select_text', 'get_cell_addr', 'is_cell'):
                        original = getattr(hwp, name, None)
                        if not callable(original):
                            continue
                        def wrap(*args, name=name, original=original, **kwargs):
                            if fired:
                                after.append(name)
                            f = inspect.currentframe().f_back
                            in_where = False
                            while f:
                                if f.f_code.co_filename.replace('\\', '/').endswith('/commands/where/run.py'):
                                    in_where = True
                                f = f.f_back
                            if name == method and in_where and not fired:
                                fired.append(len(hwp.calls))
                                if malformed:
                                    return ('bad',)
                                raise cause
                            return original(*args, **kwargs)
                        setattr(hwp, name, wrap)
                    result = run(owner, step, {'op': 'where'}, {'op': 'table_inventory'})
                    self.assertTrue(fired)
                    self.assertFalse(result['ok'])
                    self.assertEqual(len(result['steps']), 2)
                    self.assertTrue(result['steps'][0]['ok'])
                    self.assertFalse(result['steps'][1]['ok'])
                    self.assertTrue(result['steps'][1]['mutation_may_have_persisted'])
                    self.assertTrue(owner.store['working_copy_dirty'])
                    self.assertTrue(owner.session._normal_close_requires_save)
                    self.assertTrue(owner.session._native_custody_held)
                    self.assertEqual(after, [])
                    self.assertEqual(len(hwp.calls), fired[0])
                    self.assertEqual(hwp.by_inst('1012').named('B2').fill['face'], 0x0000FF)
                    error = owner.session._terminal_error
                    if not malformed:
                        self.assertIs(error.__cause__.__cause__, cause)
                    with self.assertRaises(rt.LocalCliRuntimeError):
                        owner.manager.close_session(owner.sid, timeout=1)
                    self.assertEqual(owner.cleanup_events(), [])

    def test_actual_public_step_serializer_preserves_omission_not_clean_proof(self):
        hwp = sample_document()
        owner = real_owned(self, hwp)
        owner.service._public_bundle_steps = LocalCliService._public_bundle_steps.__get__(owner.service)
        result = run(owner, {'op': 'table_inventory'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['before']['observation_status'], 'omitted_guarded_envelope')
        self.assertIsNone(result['before']['document_is_modified'])
        self.assertEqual(result['steps'][0]['before']['observation_status'], 'omitted_guarded_envelope')
        self.assertEqual(len(result['steps'][0]['result']['tables']), 7)


if __name__ == '__main__':
    unittest.main()
